"""
harness.py — instrumento de medición de UNA corrida del experimento.

Ejecuta el flujo de una corrida con los instantes acordados:
    calentamiento -> t=0 tráfico sostenido -> t=20 inyectar falla ->
    t=60 limpiar falla -> t=100 fin de corrida.

Uso:
    python harness.py \
        --tipo-falla timeout \
        --numero-corrida 1 \
        --router-url http://ms-router:8000 \
        --instancia-objetivo instancia-3 \
        --instancia-objetivo-url http://ms-cotizacion-3:5000 \
        --archivo-log /resultados/harness-corrida-timeout-1.jsonl
"""
import argparse
import json
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import requests

PAYLOAD_SINTETICO = {"cliente_id": "SINTETICO-CANARY", "producto": "viaje", "monto_asegurado": 1000000}
PRIMA_ESPERADA = 15000.0
TOLERANCIA = 0.01

TIMEOUT_CLIENTE_HARNESS_SEGUNDOS = 5.0  # hacia el Router

# Instantes de la corrida (segundos desde t=0)
SOLICITUDES_CALENTAMIENTO = 30
DURACION_CORRIDA_SEGUNDOS = 100  # t=0 a t=100
INSTANTE_INYECCION_SEGUNDOS = 20
INSTANTE_LIMPIEZA_SEGUNDOS = 60
TASA_SOLICITUDES_POR_SEGUNDO = 10

_log_lock = threading.Lock()
_contador_solicitudes_lock = threading.Lock()
_contador_solicitudes = 0
_contador_correctas = 0
_contador_incorrectas = 0


def parse_args():
    p = argparse.ArgumentParser(description="Harness de una corrida del experimento de disponibilidad")
    p.add_argument("--tipo-falla", required=True, choices=["timeout", "error_http", "prima_inconsistente"])
    p.add_argument("--numero-corrida", required=True, type=int)
    p.add_argument("--router-url", required=True)
    p.add_argument("--instancia-objetivo", required=True)
    p.add_argument("--instancia-objetivo-url", required=True)
    p.add_argument("--archivo-log", required=True)
    return p.parse_args()


def registrar_evento(archivo_log: str, evento: str, **campos):
    linea = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "componente": "harness",
        "evento": evento,
        **campos,
    }
    with _log_lock:
        with open(archivo_log, "a") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")


def siguiente_id_solicitud():
    global _contador_solicitudes
    with _contador_solicitudes_lock:
        _contador_solicitudes += 1
        return _contador_solicitudes


def contabilizar_resultado(es_correcta: bool):
    global _contador_correctas, _contador_incorrectas
    with _contador_solicitudes_lock:
        if es_correcta:
            _contador_correctas += 1
        else:
            _contador_incorrectas += 1


def enviar_solicitud_cotizacion(router_url: str, archivo_log: str):
    """Envía una solicitud al Router, mide correctness y registra el resultado."""
    id_solicitud = siguiente_id_solicitud()
    timestamp_envio = datetime.now(timezone.utc).isoformat()
    registrar_evento(archivo_log, "solicitud_enviada", id_solicitud=id_solicitud, timestamp_envio=timestamp_envio)

    inicio = time.monotonic()
    try:
        resp = requests.post(f"{router_url}/cotizar", json=PAYLOAD_SINTETICO, timeout=TIMEOUT_CLIENTE_HARNESS_SEGUNDOS)
        latencia_ms = (time.monotonic() - inicio) * 1000
        codigo_http = resp.status_code
        instancia_id_respuesta = None
        prima_recibida = None
        detalle = "ok"

        if codigo_http == 200:
            try:
                cuerpo = resp.json()
                prima_recibida = cuerpo.get("prima")
                instancia_id_respuesta = cuerpo.get("instancia_id")
            except ValueError:
                detalle = "respuesta_200_no_json"

        if codigo_http != 200:
            es_correcta = False
            detalle = f"http {codigo_http}"
        elif prima_recibida is None or abs(prima_recibida - PRIMA_ESPERADA) > TOLERANCIA:
            es_correcta = False
            if detalle == "ok":
                detalle = f"prima incorrecta: {prima_recibida}"
        else:
            es_correcta = True

    except requests.exceptions.Timeout:
        latencia_ms = (time.monotonic() - inicio) * 1000
        codigo_http = None
        prima_recibida = None
        instancia_id_respuesta = None
        es_correcta = False
        detalle = "timeout_cliente_harness"
    except requests.exceptions.RequestException as e:
        latencia_ms = (time.monotonic() - inicio) * 1000
        codigo_http = None
        prima_recibida = None
        instancia_id_respuesta = None
        es_correcta = False
        detalle = f"error_conexion: {e}"

    registrar_evento(
        archivo_log, "solicitud_resultado",
        id_solicitud=id_solicitud,
        timestamp_respuesta=datetime.now(timezone.utc).isoformat(),
        codigo_http=codigo_http,
        prima_recibida=prima_recibida,
        instancia_id_respuesta=instancia_id_respuesta,
        es_correcta=es_correcta,
        latencia_ms=latencia_ms,
        detalle=detalle,
    )
    contabilizar_resultado(es_correcta)
    return es_correcta


def calentamiento(router_url: str, archivo_log: str):
    errores = 0
    for _ in range(SOLICITUDES_CALENTAMIENTO):
        if not enviar_solicitud_cotizacion(router_url, archivo_log):
            errores += 1
        time.sleep(1.0 / TASA_SOLICITUDES_POR_SEGUNDO)
    registrar_evento(
        archivo_log, "fin_calentamiento",
        solicitudes_calentamiento=SOLICITUDES_CALENTAMIENTO, errores_calentamiento=errores,
    )
    if errores > 0:
        print(f"[ADVERTENCIA] {errores} error(es) durante el calentamiento. Revisar antes de confiar en esta corrida.")


def tráfico_sostenido(router_url: str, archivo_log: str, instancia_objetivo_url: str, tipo_falla: str, instancia_objetivo: str):
    """
    Genera tráfico a tasa constante durante DURACION_CORRIDA_SEGUNDOS, usando un pool de hilos
    para que una solicitud lenta (p.ej. durante el timeout inyectado) no retrase las siguientes.
    Los instantes de inyección/limpieza se calculan como offset desde t=0.
    """
    t0 = time.monotonic()
    inyectada = False
    limpiada = False

    with ThreadPoolExecutor(max_workers=50) as pool:
        while True:
            ahora = time.monotonic() - t0
            if ahora >= DURACION_CORRIDA_SEGUNDOS:
                break

            pool.submit(enviar_solicitud_cotizacion, router_url, archivo_log)

            if not inyectada and ahora >= INSTANTE_INYECCION_SEGUNDOS:
                inyectada = True
                url_inject = f"{instancia_objetivo_url}/inject-fault"
                codigo = _inyectar_falla_real(url_inject, tipo_falla)
                registrar_evento(
                    archivo_log, "falla_inyectada",
                    instancia_objetivo=instancia_objetivo, tipo_falla=tipo_falla, respuesta_inject_fault=codigo,
                )

            if not limpiada and ahora >= INSTANTE_LIMPIEZA_SEGUNDOS:
                limpiada = True
                url_clear = f"{instancia_objetivo_url}/clear-fault"
                codigo = _limpiar_falla_real(url_clear)
                registrar_evento(archivo_log, "falla_limpiada", instancia_objetivo=instancia_objetivo, respuesta_clear_fault=codigo)

            time.sleep(1.0 / TASA_SOLICITUDES_POR_SEGUNDO)


def _inyectar_falla_real(url: str, tipo_falla: str, reintentos: int = 2):
    for intento in range(reintentos + 1):
        try:
            resp = requests.post(url, json={"tipo": tipo_falla}, timeout=3)
            return resp.status_code
        except requests.exceptions.RequestException:
            if intento == reintentos:
                return None
            time.sleep(0.5)


def _limpiar_falla_real(url: str, reintentos: int = 2):
    for intento in range(reintentos + 1):
        try:
            resp = requests.post(url, timeout=3)
            return resp.status_code
        except requests.exceptions.RequestException:
            if intento == reintentos:
                return None
            time.sleep(0.5)


def main():
    args = parse_args()

    registrar_evento(
        args.archivo_log, "inicio_corrida",
        tipo_falla=args.tipo_falla, numero_corrida=args.numero_corrida, instancia_objetivo=args.instancia_objetivo,
    )

    print(f"[{args.tipo_falla} #{args.numero_corrida}] Calentamiento ({SOLICITUDES_CALENTAMIENTO} solicitudes)...")
    calentamiento(args.router_url, args.archivo_log)

    print(f"[{args.tipo_falla} #{args.numero_corrida}] Tráfico sostenido ({DURACION_CORRIDA_SEGUNDOS}s)...")
    tráfico_sostenido(
        args.router_url, args.archivo_log, args.instancia_objetivo_url, args.tipo_falla, args.instancia_objetivo,
    )

    registrar_evento(
        args.archivo_log, "fin_corrida",
        total_solicitudes=_contador_solicitudes,
        total_correctas=_contador_correctas,
        total_incorrectas=_contador_incorrectas,
    )
    print(f"[{args.tipo_falla} #{args.numero_corrida}] Corrida terminada. Log: {args.archivo_log}")


if __name__ == "__main__":
    main()
