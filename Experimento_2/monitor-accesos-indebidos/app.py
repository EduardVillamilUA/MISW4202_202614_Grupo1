"""
monitor-accesos-indebidos
==========================
Analiza, de forma continua y asíncrona, cada consulta de perfil que ocurre en
el sistema y decide si constituye un acceso indebido.

Aplica dos reglas independientes:

1. Una regla determinística de alcance: un actor con rol "cliente" que
   consulta un client_id distinto al suyo es, sin ambigüedad, un acceso
   indebido. No requiere estado ni historial: se resuelve con los datos que
   ya trae el propio evento.
2. Una regla heurística de volumen y diversidad, para actores con roles de
   alcance ampliado (asesor, operaciones) que no tienen un único client_id
   "propio" contra el cual comparar. Se apoya en una ventana de tiempo
   deslizante por actor, mantenida en Redis, para detectar patrones de
   consumo sospechosos (demasiadas consultas, o consultas a demasiados
   clientes distintos, en poco tiempo).

Este componente no responde directamente a ninguna solicitud del flujo
principal: consume eventos de un canal de Redis en un hilo de fondo,
independiente del ciclo de peticiones HTTP de Flask, y publica sus hallazgos
en otro canal. Los endpoints HTTP que expone son exclusivamente de
diagnóstico (verificar salud del proceso y consultar el estado vigente de la
ventana de un actor), no forman parte del camino de detección en sí.
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
import redis


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
PUERTO = int(os.environ.get("PUERTO", "8002"))

VENTANA_SEGUNDOS = int(os.environ.get("VENTANA_SEGUNDOS", "30"))
UMBRAL_VOLUMEN = int(os.environ.get("UMBRAL_VOLUMEN", "8"))
UMBRAL_DIVERSIDAD = int(os.environ.get("UMBRAL_DIVERSIDAD", "5"))

# Roles a los que se les aplica la regla heurística de volumen/diversidad en
# lugar de la regla determinística de alcance (esa última solo tiene sentido
# para roles que cuentan con un único client_id propio).
ROLES_ALCANCE_AMPLIADO = frozenset(
    rol.strip()
    for rol in os.environ.get("ROLES_ALCANCE_AMPLIADO", "asesor,operaciones").split(",")
    if rol.strip()
)

CANAL_ACCESO_PERFIL = "canal:acceso-perfil"
CANAL_HALLAZGOS = "canal:hallazgos"

# Espera entre reintentos si la suscripción al canal se cae por un problema
# transitorio de Redis. No es un valor que otro componente necesite conocer.
ESPERA_RECONEXION_SEGUNDOS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s monitor-accesos-indebidos %(levelname)s %(message)s",
)
logger = logging.getLogger("monitor-accesos-indebidos")


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

# Marca de tiempo (reloj monotónico) del último evento de acceso procesado
# con éxito. Se usa únicamente como señal de diagnóstico en /salud; no
# participa en ninguna decisión de detección.
_ultimo_evento_procesado_monotonic = None


def _formatear_timestamp(momento: datetime) -> str:
    """Serializa un datetime UTC como ISO-8601 con milisegundos y sufijo 'Z'."""
    return momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Publicación de hallazgos
# ---------------------------------------------------------------------------

def _publicar_hallazgo(evento_origen_id, actor_id, client_id_consultado, tipo_deteccion, razon, timestamp_evento_origen):
    """
    Construye y publica un evento de hallazgo. El timestamp de detección se
    captura justo antes de publicar, para que la diferencia contra el
    timestamp del evento de acceso original refleje la latencia real de
    detección.
    """
    hallazgo = {
        "evento_id": str(uuid.uuid4()),
        "evento_origen_id": evento_origen_id,
        "actor_id": actor_id,
        "client_id_consultado": client_id_consultado,
        "tipo_deteccion": tipo_deteccion,
        "razon": razon,
        "timestamp_evento_origen": timestamp_evento_origen,
        "timestamp_deteccion": _formatear_timestamp(datetime.now(timezone.utc)),
    }
    try:
        cliente_redis.publish(CANAL_HALLAZGOS, json.dumps(hallazgo))
    except redis.exceptions.RedisError as exc:
        logger.error(
            "fallo_publicacion_hallazgo actor_id=%s client_id=%s error=%s",
            actor_id, client_id_consultado, exc,
        )
        return
    logger.info(
        "hallazgo_publicado actor_id=%s client_id=%s tipo=%s razon=%s",
        actor_id, client_id_consultado, tipo_deteccion, razon,
    )


# ---------------------------------------------------------------------------
# Reglas de detección
# ---------------------------------------------------------------------------

def _evaluar_regla_heuristica(actor_id: str, client_id_consultado: str):
    """
    Registra la consulta actual en la ventana deslizante del actor y evalúa
    si, con esta nueva consulta incluida, se supera alguno de los umbrales.

    Devuelve la razón del hallazgo ("patron_volumen", "patron_diversidad" o
    "patron_volumen_y_diversidad") o None si ningún umbral fue superado.

    El miembro del sorted set incluye el timestamp además del client_id
    (formato "{timestamp_ms}:{client_id}") para que dos consultas al mismo
    cliente en el mismo milisegundo no colisionen entre sí y una de ellas se
    pierda silenciosamente.
    """
    clave = f"ventana:{actor_id}"
    timestamp_ms = int(time.time() * 1000)
    miembro = f"{timestamp_ms}:{client_id_consultado}"

    cliente_redis.zadd(clave, {miembro: timestamp_ms})
    limite_inferior = timestamp_ms - VENTANA_SEGUNDOS * 1000
    cliente_redis.zremrangebyscore(clave, "-inf", limite_inferior)
    miembros = cliente_redis.zrange(clave, 0, -1)

    consultas_totales = len(miembros)
    clientes_distintos = len({m.split(":", 1)[1] for m in miembros if ":" in m})

    hay_volumen = consultas_totales > UMBRAL_VOLUMEN
    hay_diversidad = clientes_distintos > UMBRAL_DIVERSIDAD

    if hay_volumen and hay_diversidad:
        return "patron_volumen_y_diversidad"
    if hay_volumen:
        return "patron_volumen"
    if hay_diversidad:
        return "patron_diversidad"
    return None


def _procesar_evento_acceso(evento: dict) -> None:
    """
    Aplica las dos reglas de detección a un evento de acceso ya deserializado.

    La regla determinística tiene prioridad y, cuando aplica, es concluyente:
    un actor con rol "cliente" nunca pasa además por la evaluación
    heurística, porque esa regla está pensada para roles sin un único
    client_id propio contra el cual comparar.
    """
    actor_id = evento["actor_id"]
    rol_actor = evento["rol_actor"]
    client_id_consultado = evento["client_id_consultado"]
    evento_origen_id = evento["evento_id"]
    timestamp_evento_origen = evento["timestamp_lectura"]

    if rol_actor == "cliente":
        alcance_propio = evento.get("alcance_propio")
        if client_id_consultado != alcance_propio:
            _publicar_hallazgo(
                evento_origen_id, actor_id, client_id_consultado,
                "deterministico", "violacion_alcance", timestamp_evento_origen,
            )
        return

    if rol_actor in ROLES_ALCANCE_AMPLIADO:
        razon = _evaluar_regla_heuristica(actor_id, client_id_consultado)
        if razon is not None:
            _publicar_hallazgo(
                evento_origen_id, actor_id, client_id_consultado,
                "heuristico", razon, timestamp_evento_origen,
            )


# ---------------------------------------------------------------------------
# Consumo continuo del canal de acceso a perfiles
# ---------------------------------------------------------------------------

def _consumir_canal_acceso_perfil() -> None:
    """
    Bucle de fondo que se mantiene suscrito a canal:acceso-perfil mientras el
    proceso esté vivo. Si la conexión con Redis se interrumpe, reintenta la
    suscripción tras una espera fija en lugar de terminar el hilo: una caída
    transitoria de Redis no debe tumbar la capacidad de detección del resto
    de la corrida.

    Un mensaje que no es JSON válido, o al que le falta algún campo
    requerido, se descarta y se registra en el log: la validación de lo que
    llega al canal es responsabilidad exclusiva de este consumidor, no del
    publicador.
    """
    global _ultimo_evento_procesado_monotonic

    while True:
        try:
            pubsub = cliente_redis.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(CANAL_ACCESO_PERFIL)
            logger.info("suscrito a %s", CANAL_ACCESO_PERFIL)

            for mensaje in pubsub.listen():
                payload = mensaje.get("data")
                if not isinstance(payload, str):
                    continue
                try:
                    evento = json.loads(payload)
                    _procesar_evento_acceso(evento)
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.error("evento_acceso_malformado error=%s payload=%s", exc, payload)
                    continue
                _ultimo_evento_procesado_monotonic = time.monotonic()
        except redis.exceptions.RedisError as exc:
            logger.error(
                "fallo_suscripcion_canal_acceso error=%s; reintentando en %ds",
                exc, ESPERA_RECONEXION_SEGUNDOS,
            )
            time.sleep(ESPERA_RECONEXION_SEGUNDOS)


# El hilo se crea y arranca al importar el módulo (no dentro de
# `if __name__ == "__main__"`), para que la suscripción quede activa desde
# el momento en que la aplicación arranca, sin depender de cómo se invoque
# el proceso Flask. Es un hilo daemon: no debe impedir que el proceso
# termine si el hilo principal termina.
hilo_consumidor = threading.Thread(
    target=_consumir_canal_acceso_perfil,
    name="consumidor-acceso-perfil",
    daemon=True,
)
hilo_consumidor.start()


# ---------------------------------------------------------------------------
# Aplicación Flask (solo endpoints de diagnóstico)
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/estado", methods=["GET"])
def estado():
    actor_id = request.args.get("actor_id")
    if not actor_id:
        return jsonify({"error": "actor_id_requerido"}), 400

    clave = f"ventana:{actor_id}"
    timestamp_ms = int(time.time() * 1000)
    limite_inferior = timestamp_ms - VENTANA_SEGUNDOS * 1000
    try:
        # Se purga antes de leer para que el conteo devuelto refleje el
        # estado vigente en este instante, no incluya consultas que ya
        # salieron de la ventana simplemente porque no ha llegado un nuevo
        # evento de ese actor que dispare la purga.
        cliente_redis.zremrangebyscore(clave, "-inf", limite_inferior)
        miembros = cliente_redis.zrange(clave, 0, -1)
    except redis.exceptions.RedisError as exc:
        logger.error("redis_no_disponible_consulta_estado actor_id=%s error=%s", actor_id, exc)
        return jsonify({"error": "estado_no_disponible"}), 503

    consultas_totales = len(miembros)
    clientes_distintos = len({m.split(":", 1)[1] for m in miembros if ":" in m})

    return jsonify({
        "actor_id": actor_id,
        "ventana_segundos": VENTANA_SEGUNDOS,
        "umbral_volumen": UMBRAL_VOLUMEN,
        "umbral_diversidad": UMBRAL_DIVERSIDAD,
        "consultas_totales_vigentes": consultas_totales,
        "clientes_distintos_vigentes": clientes_distintos,
    }), 200


@app.route("/salud", methods=["GET"])
def salud():
    try:
        cliente_redis.ping()
    except redis.exceptions.RedisError:
        return jsonify({"estado": "redis_no_disponible"}), 503

    if not hilo_consumidor.is_alive():
        return jsonify({"estado": "hilo_consumidor_caido"}), 503

    cuerpo = {"estado": "ok"}
    if _ultimo_evento_procesado_monotonic is not None:
        cuerpo["segundos_desde_ultimo_evento"] = round(
            time.monotonic() - _ultimo_evento_procesado_monotonic, 2
        )
    return jsonify(cuerpo), 200


if __name__ == "__main__":
    logger.info("monitor-accesos-indebidos iniciando en el puerto %d", PUERTO)
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)
