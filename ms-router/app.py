"""
ms-router
=========
Fachada única de enrutamiento para el experimento de disponibilidad de Solventa.
Implementa HA-DISP-14 (enmascaramiento): reenvía cada solicitud POST /cotizar
únicamente a instancias marcadas como "healthy" en el Registro de Salud (Redis),
usando round-robin sobre las instancias saludables del momento.

Responsable: Eduard.
"""

import os
import json
import time
import uuid
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import redis
import requests


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

def parsear_instancias(valor: str) -> dict:
    """
    Convierte 'instancia-1=http://ms-cotizacion-1:5000,instancia-2=...'
    en un diccionario {instancia_id: url_base}.

    Se preserva el ORDEN en que aparecen en la variable de entorno porque
    ese orden es el que usa el round-robin cuando se filtran las instancias
    saludables.
    """
    instancias = {}
    for par in valor.split(","):
        par = par.strip()
        if not par:
            continue
        instancia_id, url = par.split("=", 1)
        instancias[instancia_id.strip()] = url.strip().rstrip("/")
    if not instancias:
        raise ValueError("La variable INSTANCIAS está vacía o mal formada")
    return instancias


INSTANCIAS = parsear_instancias(os.environ["INSTANCIAS"])
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
TIMEOUT_FORWARD_SEGUNDOS = float(os.environ.get("TIMEOUT_FORWARD_SEGUNDOS", "3"))
PUERTO = int(os.environ.get("PUERTO", "8000"))
ARCHIVO_LOG = os.environ.get("ARCHIVO_LOG", "/resultados/ms-router.jsonl")


# ---------------------------------------------------------------------------
# Registro de eventos JSON Lines
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def log_evento(evento: str, **campos) -> None:
    """
    Escribe una línea JSON en ARCHIVO_LOG con los campos obligatorios
    (timestamp UTC ISO-8601 con microsegundos, componente, evento) más
    los campos específicos del evento.
    """
    registro = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "componente": "ms-router",
        "evento": evento,
        **campos,
    }
    linea = json.dumps(registro, ensure_ascii=False)
    with _log_lock:
        # Modo "a" (append): cada corrida debe limpiar/renombrar este archivo
        # ANTES de arrancar el contenedor.
        with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")


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


def leer_estados() -> dict:
    """
    Lee, en este momento y sin usar ningún valor guardado de una lectura
    anterior, el estado de las tres claves solventa:health:instancia-N.

    Si una clave no existe todavía (None) o tiene un valor distinto de
    "healthy", la instancia se trata como NO disponible. Nunca se asume
    que una instancia sin dato en Redis está sana.
    """
    estados = {}
    for instancia_id in INSTANCIAS.keys():
        clave = f"solventa:health:{instancia_id}"
        valor = cliente_redis.get(clave)
        estados[instancia_id] = "healthy" if valor == "healthy" else "unhealthy"
    return estados


# ---------------------------------------------------------------------------
# 4. Round-robin sobre instancias saludables
# ---------------------------------------------------------------------------

_rr_lock = threading.Lock()
_rr_contador = 0


def elegir_round_robin(saludables_en_orden: list) -> str:
    """
    Elige una instancia por turno rotativo, considerando ÚNICAMENTE la
    lista de instancias saludables recibida (no las tres totales). El
    contador es compartido entre solicitudes para repartir la carga.
    """
    global _rr_contador
    with _rr_lock:
        idx = _rr_contador % len(saludables_en_orden)
        _rr_contador += 1
    return saludables_en_orden[idx]


# ---------------------------------------------------------------------------
# Aplicación Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/cotizar", methods=["POST"])
def cotizar():
    id_solicitud = uuid.uuid4().hex[:8]
    log_evento("solicitud_recibida", id_solicitud=id_solicitud)

    cuerpo = request.get_json(silent=True)
    if cuerpo is None:
        cuerpo = {}

    # --- Leer Redis sin caché y construir lista de saludables ---
    estados = leer_estados()
    log_evento("estado_leido", id_solicitud=id_solicitud, estados=estados)

    saludables = [iid for iid in INSTANCIAS.keys() if estados[iid] == "healthy"]

    # --- Sin instancias saludables -> 503 inmediato ---
    if not saludables:
        log_evento("solicitud_rechazada_sin_instancias", id_solicitud=id_solicitud)
        return jsonify({"error": "no hay instancias saludables disponibles"}), 503

    # --- Elegir por round-robin entre las saludables ---
    instancia_elegida = elegir_round_robin(saludables)
    url_destino = f"{INSTANCIAS[instancia_elegida]}/cotizar"

    # --- Reenviar y devolver la respuesta tal cual ---
    inicio = time.monotonic()
    try:
        resp = requests.post(
            url_destino,
            json=cuerpo,
            timeout=TIMEOUT_FORWARD_SEGUNDOS,
        )
        latencia_ms = round((time.monotonic() - inicio) * 1000, 2)

        #   - "ok"             -> la instancia respondió con HTTP < 400
        #   - "error_instancia"-> la instancia respondió, pero con HTTP >= 400
        #                         (p. ej. falla error_http inyectada -> 500)
        #   - "error_502"      -> el Router NO logró obtener respuesta
        #                         (timeout o conexión rechazada) y fabricó
        #                         él mismo el 502 (ver bloque except abajo)
        resultado = "ok" if resp.status_code < 400 else "error_instancia"

        log_evento(
            "solicitud_enrutada",
            id_solicitud=id_solicitud,
            instancia_elegida=instancia_elegida,
            resultado=resultado,
            codigo_http_respuesta=resp.status_code,
            latencia_ms=latencia_ms,
        )
        return (resp.content, resp.status_code, {"Content-Type": "application/json"})

    except requests.exceptions.RequestException:
        # Timeout o conexión rechazada: exactamente el evento que mide la
        # "ventana de exposición".
        latencia_ms = round((time.monotonic() - inicio) * 1000, 2)
        log_evento(
            "solicitud_enrutada",
            id_solicitud=id_solicitud,
            instancia_elegida=instancia_elegida,
            resultado="error_502",
            codigo_http_respuesta=502,
            latencia_ms=latencia_ms,
        )
        return jsonify({
            "error": "instancia no respondió",
            "instancia_id": instancia_elegida,
        }), 502


if __name__ == "__main__":
    log_evento("inicio", instancias=list(INSTANCIAS.keys()))
    # Servidor de desarrollo de Flask: suficiente para el volumen del
    # experimento (10 solicitudes/segundo, un único host). No se usa
    # un servidor WSGI de producción porque el alcance es un experimento
    # controlado, no un despliegue real de Solventa.
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)