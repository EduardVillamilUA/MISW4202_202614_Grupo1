"""
ms-cotizacion — servicio de negocio bajo prueba (HA-DISP-13 / HA-DISP-14).
Servicio de cotización con inyección controlada de fallas para el experimento
de disponibilidad. Se despliega 3 veces (misma imagen, distinta INSTANCIA_ID).
"""
import os
import json
import time
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)

INSTANCIA_ID = os.environ["INSTANCIA_ID"]
PUERTO = int(os.environ.get("PUERTO", "5000"))
ARCHIVO_LOG = os.environ["ARCHIVO_LOG"]

TASAS = {"viaje": 0.015, "dispositivo": 0.03, "vida": 0.008}

falla_activa = None  # None | "timeout" | "error_http" | "prima_inconsistente"
_lock = threading.Lock()
_log_lock = threading.Lock()


def registrar_evento(evento: str, **campos):
    linea = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "componente": "ms-cotizacion",
        "evento": evento,
        **campos,
    }
    with _log_lock:
        with open(ARCHIVO_LOG, "a") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")


@app.route("/health", methods=["GET"])
def health():
    with _lock:
        estado = falla_activa

    if estado == "timeout":
        registrar_evento("health_check_recibido", instancia_id=INSTANCIA_ID, resultado="timeout")
        time.sleep(10)
        return jsonify({"status": "ok", "instancia_id": INSTANCIA_ID}), 200

    if estado == "error_http":
        registrar_evento("health_check_recibido", instancia_id=INSTANCIA_ID, resultado="error")
        return jsonify({"status": "error", "instancia_id": INSTANCIA_ID}), 500

    registrar_evento("health_check_recibido", instancia_id=INSTANCIA_ID, resultado="ok")
    return jsonify({"status": "ok", "instancia_id": INSTANCIA_ID}), 200


@app.route("/cotizar", methods=["POST"])
def cotizar():
    body = request.get_json(force=True, silent=True) or {}
    cliente_id = body.get("cliente_id")
    producto = body.get("producto")
    monto_asegurado = body.get("monto_asegurado")
    es_sintetica = cliente_id == "SINTETICO-CANARY"

    with _lock:
        estado = falla_activa

    if estado == "timeout":
        time.sleep(10)

    if estado == "error_http":
        registrar_evento(
            "cotizacion_recibida", instancia_id=INSTANCIA_ID, cliente_id=cliente_id,
            producto=producto, monto_asegurado=monto_asegurado, prima_calculada=None,
            es_sintetica=es_sintetica, resultado="error",
        )
        return jsonify({"error": "fallo interno simulado", "instancia_id": INSTANCIA_ID}), 500

    if producto not in TASAS:
        registrar_evento(
            "cotizacion_recibida", instancia_id=INSTANCIA_ID, cliente_id=cliente_id,
            producto=producto, monto_asegurado=monto_asegurado, prima_calculada=None,
            es_sintetica=es_sintetica, resultado="error",
        )
        return jsonify({"error": "producto no soportado"}), 400

    prima = monto_asegurado * TASAS[producto]
    if estado == "prima_inconsistente":
        prima = prima * 1.5

    resultado_evento = "timeout" if estado == "timeout" else "ok"
    registrar_evento(
        "cotizacion_recibida", instancia_id=INSTANCIA_ID, cliente_id=cliente_id,
        producto=producto, monto_asegurado=monto_asegurado, prima_calculada=prima,
        es_sintetica=es_sintetica, resultado=resultado_evento,
    )

    return jsonify({
        "cliente_id": cliente_id,
        "producto": producto,
        "monto_asegurado": monto_asegurado,
        "prima": prima,
        "instancia_id": INSTANCIA_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 200


# SOLO PARA EL EXPERIMENTO — NO USAR EN PRODUCCIÓN
@app.route("/inject-fault", methods=["POST"])
def inject_fault():
    global falla_activa
    body = request.get_json(force=True, silent=True) or {}
    tipo = body.get("tipo")
    if tipo not in ("timeout", "error_http", "prima_inconsistente"):
        return jsonify({"error": "tipo de falla no soportado"}), 400
    with _lock:
        falla_activa = tipo
    registrar_evento("falla_inyectada", instancia_id=INSTANCIA_ID, tipo=tipo)
    return jsonify({"status": "fault_injected", "tipo": tipo, "instancia_id": INSTANCIA_ID}), 200


# SOLO PARA EL EXPERIMENTO — NO USAR EN PRODUCCIÓN
@app.route("/clear-fault", methods=["POST"])
def clear_fault():
    global falla_activa
    with _lock:
        falla_activa = None
    registrar_evento("falla_limpiada", instancia_id=INSTANCIA_ID)
    return jsonify({"status": "fault_cleared", "instancia_id": INSTANCIA_ID}), 200


if __name__ == "__main__":
    registrar_evento("inicio", instancia_id=INSTANCIA_ID)
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)
