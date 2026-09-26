"""
servicio-perfilamiento
=======================
Resuelve la lectura de un perfil de riesgo simulado y, en paralelo, publica un
evento de auditoría con los datos de esa consulta.

El punto de diseño central de este componente es la separación entre el
camino síncrono (responder al solicitante con el perfil) y el camino
asíncrono (dejar constancia de que la consulta ocurrió, para que otro
componente decida más tarde, sin apuro, si el patrón de acceso es indebido).
La respuesta HTTP nunca debe esperar a que la publicación en Redis se
complete: si Redis está lento o caído en ese instante, el solicitante igual
debe recibir su respuesta a tiempo.

Este servicio no llama a ningún otro servicio HTTP. Su única salida hacia el
resto del sistema es el evento que publica en Redis; no sabe ni le importa
quién lo consume.
"""

import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, jsonify, request
import redis


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
PUERTO = int(os.environ.get("PUERTO", "8001"))

CANAL_ACCESO_PERFIL = "canal:acceso-perfil"

# Encabezados que api-gateway ya resolvió antes de reenviar la solicitud.
# Este servicio confía en ellos tal cual: no vuelve a resolver rol ni
# alcance, solo los repite dentro del evento que publica.
ENCABEZADO_ACTOR_ID = "X-Actor-Id"
ENCABEZADO_ACTOR_ROL = "X-Actor-Rol"
ENCABEZADO_ACTOR_ALCANCE_PROPIO = "X-Actor-Alcance-Propio"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s servicio-perfilamiento %(levelname)s %(message)s",
)
logger = logging.getLogger("servicio-perfilamiento")


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

# La publicación del evento de acceso se delega a un hilo aparte para
# garantizar, incluso si PUBLISH tardara más de lo normal, que nunca retrase
# el envío de la respuesta al solicitante. Un pool pequeño evita crear un
# hilo nuevo por cada solicitud bajo ráfagas de tráfico, sin introducir cola
# de espera perceptible para el volumen de este experimento.
_publicador_eventos = ThreadPoolExecutor(max_workers=8, thread_name_prefix="publicador-eventos")


def _formatear_timestamp(momento: datetime) -> str:
    """Serializa un datetime UTC como ISO-8601 con milisegundos y sufijo 'Z'."""
    return momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"


def _publicar_evento_acceso(actor_id, rol_actor, alcance_propio_header, client_id, momento_lectura):
    """
    Construye y publica el evento de acceso. Se ejecuta en un hilo del pool,
    después de que la respuesta HTTP ya fue enviada.

    Un fallo aquí (por ejemplo, Redis momentáneamente inalcanzable) se
    registra en el log y no se reintenta ni se propaga: la solicitud
    original ya fue atendida y no hay a quién devolverle este error.
    """
    alcance_propio = alcance_propio_header if rol_actor == "cliente" else None
    evento = {
        "evento_id": str(uuid.uuid4()),
        "actor_id": actor_id,
        "rol_actor": rol_actor,
        "alcance_propio": alcance_propio,
        "client_id_consultado": client_id,
        "timestamp_lectura": _formatear_timestamp(momento_lectura),
    }
    try:
        cliente_redis.publish(CANAL_ACCESO_PERFIL, json.dumps(evento))
    except redis.exceptions.RedisError as exc:
        logger.error(
            "fallo_publicacion_evento_acceso actor_id=%s client_id=%s error=%s",
            actor_id, client_id, exc,
        )


# ---------------------------------------------------------------------------
# Aplicación Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/perfil/<client_id>", methods=["GET"])
def perfil(client_id):
    actor_id = request.headers.get(ENCABEZADO_ACTOR_ID)
    rol_actor = request.headers.get(ENCABEZADO_ACTOR_ROL)
    # Se distingue "encabezado ausente" (None) de "presente pero vacío" (""),
    # porque una cadena vacía es un valor legítimo de X-Actor-Alcance-Propio
    # para actores que no son de rol "cliente".
    alcance_propio_header = request.headers.get(ENCABEZADO_ACTOR_ALCANCE_PROPIO)

    if actor_id is None or rol_actor is None or alcance_propio_header is None:
        return jsonify({"error": "encabezados_incompletos"}), 400

    try:
        datos_perfil = cliente_redis.hgetall(f"perfil:{client_id}")
    except redis.exceptions.RedisError as exc:
        logger.error("redis_no_disponible_lectura_perfil client_id=%s error=%s", client_id, exc)
        return jsonify({"error": "perfil_no_disponible"}), 503

    # El instante de lectura se captura aquí, justo tras resolver el dato en
    # Redis, no más adelante cuando se publique el evento: es el momento que
    # realmente describe cuándo ocurrió la consulta.
    momento_lectura = datetime.now(timezone.utc)

    if datos_perfil:
        codigo = 200
        cuerpo = datos_perfil
    else:
        # Un perfil inexistente también se audita: no se omite la
        # publicación del evento solo porque la consulta no tuvo datos que
        # devolver.
        codigo = 404
        cuerpo = {"error": "perfil_no_encontrado"}

    _publicador_eventos.submit(
        _publicar_evento_acceso, actor_id, rol_actor, alcance_propio_header, client_id, momento_lectura
    )

    return jsonify(cuerpo), codigo


@app.route("/salud", methods=["GET"])
def salud():
    try:
        cliente_redis.ping()
    except redis.exceptions.RedisError:
        return jsonify({"estado": "redis_no_disponible"}), 503
    return jsonify({"estado": "ok"}), 200


if __name__ == "__main__":
    logger.info("servicio-perfilamiento iniciando en el puerto %d", PUERTO)
    # Servidor de desarrollo de Flask con threaded=True: cada solicitud se
    # atiende en su propio hilo, lo que además permite que el ThreadPoolExecutor
    # de publicación de eventos no compita por el único hilo de un servidor
    # WSGI de un solo worker.
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)
