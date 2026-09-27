"""
orquestador-reaccion
=====================
Se suscribe de forma continua a los hallazgos de patrones de acceso indebido
y, por cada uno, ejecuta la reacción de seguridad (bloquear al actor) de
forma idempotente, dejando constancia en el Registro de Auditoría tanto del
hallazgo recibido como de la reacción ejecutada.

La suscripción corre en un hilo de fondo separado del servidor Flask: este
último solo expone un par de endpoints de diagnóstico (`/bloqueados` y
`/salud`). El hilo de fondo se reconecta solo si pierde la conexión con
Redis, porque debe permanecer escuchando durante toda la vida del proceso,
no solo durante el ciclo de una solicitud HTTP.

La acción de seguridad (agregar al actor al conjunto de bloqueados) nunca
debe depender de que el registro de auditoría haya podido escribirse: si
`registro-auditoria` no responde, el bloqueo igual se ejecuta y el intento
fallido de auditoría queda en el log local para poder reconstruirlo
manualmente si hace falta.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify
import redis
import requests


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
URL_REGISTRO_AUDITORIA = os.environ.get(
    "URL_REGISTRO_AUDITORIA", "http://registro-auditoria:8004"
)
TTL_PROCESADOS_SEGUNDOS = int(os.environ.get("TTL_PROCESADOS_SEGUNDOS", "3600"))
PUERTO = int(os.environ.get("PUERTO", "8003"))

CANAL_HALLAZGOS = "canal:hallazgos"

# Detalles internos de este componente, fijados en el código porque ningún
# otro componente necesita conocerlos ni configurarlos.
TIMEOUT_AUDITORIA_SEGUNDOS = 3
ESPERA_REINTENTO_AUDITORIA_SEGUNDOS = 0.2
ESPERA_RECONEXION_REDIS_SEGUNDOS = 2
TIMEOUT_SALUD_AUDITORIA_SEGUNDOS = 2

CAMPOS_HALLAZGO_REQUERIDOS = (
    "evento_id",
    "actor_id",
    "client_id_consultado",
    "tipo_deteccion",
    "razon",
    "timestamp_evento_origen",
    "timestamp_deteccion",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s orquestador-reaccion %(levelname)s %(message)s",
)
logger = logging.getLogger("orquestador-reaccion")


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

# Cliente Redis independiente, exclusivo del hilo de fondo que hace
# pubsub.listen(). Esa llamada debe poder bloquear indefinidamente mientras
# espera el próximo hallazgo: un silencio de varios segundos entre hallazgos
# (lo normal, no un síntoma de falla) es indistinguible, con un
# socket_timeout corto, de una conexión realmente caída, y provoca una
# resuscripción constante. Compartir el cliente_redis de arriba (pensado
# para comandos puntuales rápidos desde los endpoints HTTP y las acciones de
# reacción) además dejaría, en cada uno de esos timeouts espurios, una
# conexión a medio leer en el pool compartido, que un request HTTP
# posterior podría heredar y quedar colgado. Sin socket_timeout, listen()
# solo levanta una excepción ante una caída real de la conexión (por
# ejemplo, si Redis se reinicia), que es el único caso que este hilo
# necesita manejar reconectando.
cliente_redis_pubsub = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=5,
)

# Señal de vida del hilo consumidor: se marca cada vez que el hilo logra
# suscribirse a Redis, y se limpia si pierde la conexión. /salud la usa para
# distinguir "el proceso está vivo" de "el proceso está realmente escuchando".
_consumidor_activo = threading.Event()


def _formatear_timestamp(momento: datetime) -> str:
    """Serializa un datetime UTC como ISO-8601 con milisegundos y sufijo 'Z'."""
    return momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Registro de auditoría (con reintento)
# ---------------------------------------------------------------------------

def _registrar_en_auditoria(payload: dict) -> None:
    """
    Envía un registro a registro-auditoria, reintentando una vez tras una
    espera corta si el primer intento falla.

    Deliberadamente no lanza excepciones hacia el llamador: la persistencia
    de auditoría es importante para las métricas del experimento, pero no
    puede convertirse en un motivo para que la reacción de seguridad (el
    bloqueo del actor) se retrase o se aborte. Si ambos intentos fallan, se
    registra el payload completo en el log local para poder reconstruirlo
    manualmente.
    """
    url = f"{URL_REGISTRO_AUDITORIA}/eventos"
    for intento in (1, 2):
        try:
            respuesta = requests.post(url, json=payload, timeout=TIMEOUT_AUDITORIA_SEGUNDOS)
            if respuesta.status_code >= 500:
                raise requests.exceptions.RequestException(
                    f"registro-auditoria respondió {respuesta.status_code}"
                )
            return
        except requests.exceptions.RequestException as exc:
            if intento == 1:
                logger.warning(
                    "fallo_registro_auditoria_reintentando evento_id=%s tipo_registro=%s error=%s",
                    payload.get("evento_id"), payload.get("tipo_registro"), exc,
                )
                time.sleep(ESPERA_REINTENTO_AUDITORIA_SEGUNDOS)
            else:
                logger.error(
                    "fallo_registro_auditoria_definitivo payload=%s error=%s", payload, exc
                )


# ---------------------------------------------------------------------------
# Procesamiento de cada hallazgo recibido
# ---------------------------------------------------------------------------

def _procesar_hallazgo(mensaje_raw: str) -> None:
    try:
        hallazgo = json.loads(mensaje_raw)
    except json.JSONDecodeError as exc:
        logger.error("hallazgo_malformado_json contenido=%s error=%s", mensaje_raw, exc)
        return

    faltantes = [campo for campo in CAMPOS_HALLAZGO_REQUERIDOS if campo not in hallazgo]
    if faltantes:
        logger.error("hallazgo_incompleto faltantes=%s hallazgo=%s", faltantes, hallazgo)
        return

    evento_id = hallazgo["evento_id"]
    actor_id = hallazgo["actor_id"]

    # Paso 1: se registra el hallazgo tal como llegó, antes de actuar. La
    # reacción todavía no ocurrió, así que timestamp_reaccion es nulo.
    _registrar_en_auditoria({
        "evento_id": evento_id,
        "tipo_registro": "hallazgo",
        "actor_id": actor_id,
        "client_id_consultado": hallazgo["client_id_consultado"],
        "tipo_deteccion": hallazgo["tipo_deteccion"],
        "razon": hallazgo["razon"],
        "timestamp_evento_origen": hallazgo["timestamp_evento_origen"],
        "timestamp_deteccion": hallazgo["timestamp_deteccion"],
        "timestamp_reaccion": None,
        "es_duplicado": False,
    })

    # Paso 2: control de idempotencia. SET ... NX solo escribe si la clave
    # no existía; si ya existía, este mismo evento_id ya fue procesado antes
    # y no debe repetirse la acción de bloqueo.
    clave_procesado = f"procesados:{evento_id}"
    try:
        es_primera_vez = cliente_redis.set(
            clave_procesado, "1", nx=True, ex=TTL_PROCESADOS_SEGUNDOS
        )
    except redis.exceptions.RedisError as exc:
        # Sin poder confirmar el estado de deduplicación, se prefiere
        # arriesgar un bloqueo redundante (inofensivo, porque SADD es
        # idempotente) a arriesgar no bloquear a un actor que debería
        # quedar bloqueado.
        logger.error(
            "fallo_chequeo_idempotencia evento_id=%s actor_id=%s error=%s",
            evento_id, actor_id, exc,
        )
        es_primera_vez = True

    if not es_primera_vez:
        momento_reaccion = datetime.now(timezone.utc)
        logger.info("hallazgo_duplicado_omitido evento_id=%s actor_id=%s", evento_id, actor_id)
        _registrar_en_auditoria({
            "evento_id": evento_id,
            "tipo_registro": "reaccion",
            "actor_id": actor_id,
            "client_id_consultado": hallazgo["client_id_consultado"],
            "tipo_deteccion": hallazgo["tipo_deteccion"],
            "razon": hallazgo["razon"],
            "timestamp_evento_origen": hallazgo["timestamp_evento_origen"],
            "timestamp_deteccion": hallazgo["timestamp_deteccion"],
            "timestamp_reaccion": _formatear_timestamp(momento_reaccion),
            "es_duplicado": True,
        })
        return

    # Paso 3: la reacción real. Revocar la credencial simulada y bloquear al
    # actor se modelan como la misma acción observable, porque en este
    # experimento no existe un autorizador real que emita o revoque tokens:
    # agregar al actor al conjunto "bloqueados" es, en sí mismo, lo que hace
    # que el Gateway deje de atenderlo.
    try:
        cliente_redis.sadd("bloqueados", actor_id)
    except redis.exceptions.RedisError as exc:
        logger.critical(
            "fallo_bloqueo_actor evento_id=%s actor_id=%s error=%s", evento_id, actor_id, exc
        )

    # Paso 4: registrar la reacción ya ejecutada.
    momento_reaccion = datetime.now(timezone.utc)
    _registrar_en_auditoria({
        "evento_id": evento_id,
        "tipo_registro": "reaccion",
        "actor_id": actor_id,
        "client_id_consultado": hallazgo["client_id_consultado"],
        "tipo_deteccion": hallazgo["tipo_deteccion"],
        "razon": hallazgo["razon"],
        "timestamp_evento_origen": hallazgo["timestamp_evento_origen"],
        "timestamp_deteccion": hallazgo["timestamp_deteccion"],
        "timestamp_reaccion": _formatear_timestamp(momento_reaccion),
        "es_duplicado": False,
    })


# ---------------------------------------------------------------------------
# Hilo de fondo: suscripción continua a canal:hallazgos
# ---------------------------------------------------------------------------

def _consumir_hallazgos() -> None:
    """
    Bucle de vida completa del proceso: se mantiene suscrito a
    canal:hallazgos y, si la conexión con Redis se pierde, espera un momento
    y vuelve a suscribirse en lugar de terminar el hilo. Un error al
    procesar un mensaje puntual no debe tumbar la suscripción completa.
    """
    while True:
        pubsub = None
        try:
            pubsub = cliente_redis_pubsub.pubsub()
            pubsub.subscribe(CANAL_HALLAZGOS)
            _consumidor_activo.set()
            logger.info("suscrito_a_canal canal=%s", CANAL_HALLAZGOS)

            for mensaje in pubsub.listen():
                if mensaje["type"] != "message":
                    continue
                try:
                    _procesar_hallazgo(mensaje["data"])
                except Exception:
                    logger.exception("fallo_inesperado_procesando_hallazgo mensaje=%s", mensaje)
        except redis.exceptions.RedisError as exc:
            _consumidor_activo.clear()
            if pubsub is not None:
                pubsub.close()
            logger.error(
                "conexion_redis_perdida_reintentando error=%s espera_s=%s",
                exc, ESPERA_RECONEXION_REDIS_SEGUNDOS,
            )
            time.sleep(ESPERA_RECONEXION_REDIS_SEGUNDOS)


_hilo_consumidor = threading.Thread(
    target=_consumir_hallazgos, name="consumidor-hallazgos", daemon=True
)
_hilo_consumidor.start()


# ---------------------------------------------------------------------------
# Aplicación Flask (solo endpoints de diagnóstico)
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/bloqueados", methods=["GET"])
def bloqueados():
    try:
        actores = sorted(cliente_redis.smembers("bloqueados"))
    except redis.exceptions.RedisError as exc:
        logger.error("redis_no_disponible_lectura_bloqueados error=%s", exc)
        return jsonify({"error": "redis_no_disponible"}), 503
    return jsonify({"actores_bloqueados": actores}), 200


@app.route("/salud", methods=["GET"])
def salud():
    try:
        cliente_redis.ping()
    except redis.exceptions.RedisError:
        return jsonify({"estado": "redis_no_disponible"}), 503

    if not _consumidor_activo.is_set():
        return jsonify({"estado": "consumidor_hallazgos_inactivo"}), 503

    try:
        requests.get(
            f"{URL_REGISTRO_AUDITORIA}/salud", timeout=TIMEOUT_SALUD_AUDITORIA_SEGUNDOS
        ).raise_for_status()
    except requests.exceptions.RequestException:
        return jsonify({"estado": "registro_auditoria_no_disponible"}), 503

    return jsonify({"estado": "ok"}), 200


if __name__ == "__main__":
    logger.info("orquestador-reaccion iniciando en el puerto %d", PUERTO)
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)
