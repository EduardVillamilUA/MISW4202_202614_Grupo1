"""
api-gateway
===========
Punto único de entrada síncrono para las consultas de perfil de riesgo.

Es un *stub* deliberadamente simple: representa el resultado ya resuelto de
autenticación y autorización real (que se da por sentada en este experimento),
no un Gateway de producción. Antes de reenviar una solicitud resuelve la
identidad del actor contra un directorio estático, verifica que no esté
bloqueado y, si todo está en orden, reenvía la solicitud al servicio de
perfilamiento agregando los encabezados internos que ese servicio necesita.

Deliberadamente NO decide si una consulta es indebida por alcance o por
patrón de consumo: eso ocurre de forma asíncrona en otro componente, después
de que la solicitud ya fue atendida. Adelantar esa verificación aquí
penalizaría la latencia del camino síncrono, que es justo lo que no debe
pasar.
"""

import json
import logging
import os
import sys
import time

from flask import Flask, jsonify, request
import redis
import requests


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
RUTA_DIRECTORIO_ACTORES = os.environ.get(
    "RUTA_DIRECTORIO_ACTORES", "/app/config/actores.json"
)
URL_SERVICIO_PERFILAMIENTO = os.environ.get(
    "URL_SERVICIO_PERFILAMIENTO", "http://servicio-perfilamiento:8001"
)
PUERTO = int(os.environ.get("PUERTO", "8000"))

# Roles válidos del directorio estático. Solo "cliente" tiene un client_id
# propio no nulo; es el único que puede consultar su propio perfil sin
# activar la regla de alcance ampliado en el resto del sistema.
ROLES_VALIDOS = {"cliente", "asesor", "operaciones"}

# Timeout al reenviar hacia servicio-perfilamiento. Es un detalle de
# implementación interno del Gateway (no un valor que otro componente lea),
# por eso no está expuesto como variable de entorno propia del contrato.
TIMEOUT_FORWARD_SEGUNDOS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s api-gateway %(levelname)s %(message)s",
)
logger = logging.getLogger("api-gateway")


# ---------------------------------------------------------------------------
# Carga del directorio estático de actores
# ---------------------------------------------------------------------------

def cargar_directorio_actores(ruta: str) -> dict:
    """
    Lee y valida el directorio de actores una única vez al arrancar.

    Se falla de forma explícita (log de error + salida distinta de cero) si
    el archivo no existe o su contenido no respeta el esquema esperado, en
    lugar de arrancar en un estado parcialmente funcional que fallaría de
    forma confusa en cada solicitud.
    """
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            directorio = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.critical("No se pudo leer el directorio de actores en %s: %s", ruta, exc)
        sys.exit(1)

    if not isinstance(directorio, dict) or not directorio:
        logger.critical("El directorio de actores en %s está vacío o no es un objeto JSON", ruta)
        sys.exit(1)

    for actor_id, datos in directorio.items():
        if not isinstance(datos, dict) or datos.get("rol") not in ROLES_VALIDOS:
            logger.critical(
                "Entrada inválida para '%s' en el directorio de actores: %s", actor_id, datos
            )
            sys.exit(1)
        if datos["rol"] != "cliente" and datos.get("client_id_propio") is not None:
            logger.critical(
                "El actor '%s' tiene rol '%s' pero declara client_id_propio no nulo",
                actor_id, datos["rol"],
            )
            sys.exit(1)

    logger.info("Directorio de actores cargado: %d actores", len(directorio))
    return directorio


DIRECTORIO_ACTORES = cargar_directorio_actores(RUTA_DIRECTORIO_ACTORES)


# ---------------------------------------------------------------------------
# Cliente Redis
# ---------------------------------------------------------------------------

cliente_redis = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_timeout=2,
    socket_connect_timeout=2,
)


def actor_esta_bloqueado(actor_id: str) -> bool:
    return bool(cliente_redis.sismember("bloqueados", actor_id))


# ---------------------------------------------------------------------------
# Aplicación Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/perfil/<client_id>", methods=["GET"])
def perfil(client_id):
    inicio = time.monotonic()

    actor_id = request.headers.get("X-Actor-Id")
    if not actor_id:
        return jsonify({"error": "encabezado_actor_faltante"}), 400

    actor = DIRECTORIO_ACTORES.get(actor_id)
    if actor is None:
        logger.info("actor_no_reconocido actor_id=%s", actor_id)
        return jsonify({"error": "actor_no_reconocido"}), 401

    # Se consulta Redis en cada solicitud (sin caché local): el bloqueo pudo
    # haberse decidido en cualquier momento entre dos solicitudes del mismo
    # actor y debe aplicarse de inmediato.
    if actor_esta_bloqueado(actor_id):
        logger.info("actor_bloqueado actor_id=%s client_id=%s", actor_id, client_id)
        return jsonify({"error": "actor_bloqueado"}), 403

    rol = actor["rol"]
    alcance_propio = actor.get("client_id_propio") if rol == "cliente" else ""
    if alcance_propio is None:
        alcance_propio = ""

    try:
        resp = requests.get(
            f"{URL_SERVICIO_PERFILAMIENTO}/perfil/{client_id}",
            headers={
                "X-Actor-Id": actor_id,
                "X-Actor-Rol": rol,
                "X-Actor-Alcance-Propio": alcance_propio,
            },
            timeout=TIMEOUT_FORWARD_SEGUNDOS,
        )
    except requests.exceptions.RequestException as exc:
        logger.error("servicio_perfilamiento_no_disponible actor_id=%s error=%s", actor_id, exc)
        return jsonify({"error": "servicio_perfilamiento_no_disponible"}), 503
    finally:
        latencia_ms = round((time.monotonic() - inicio) * 1000, 2)
        logger.info(
            "solicitud_perfil actor_id=%s rol=%s client_id=%s latencia_ms=%s",
            actor_id, rol, client_id, latencia_ms,
        )

    # La respuesta de servicio-perfilamiento se devuelve tal cual: el
    # Gateway no reinterpreta ni transforma el código de estado ni el cuerpo.
    return (resp.content, resp.status_code, {"Content-Type": "application/json"})


@app.route("/salud", methods=["GET"])
def salud():
    try:
        cliente_redis.ping()
    except redis.exceptions.RedisError:
        return jsonify({"estado": "redis_no_disponible"}), 503
    return jsonify({"estado": "ok"}), 200


if __name__ == "__main__":
    logger.info("api-gateway iniciando en el puerto %d", PUERTO)
    # Servidor de desarrollo de Flask: suficiente para el volumen del
    # experimento (tráfico sintético de un solo harness, no producción).
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)
