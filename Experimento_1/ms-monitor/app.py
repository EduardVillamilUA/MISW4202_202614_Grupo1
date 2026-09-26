"""
ms-monitor — Monitor de Salud de Solventa
Experimento 1: Disponibilidad (HA-DISP-13 detección / HA-DISP-14 enmascaramiento)

Responsable: Fredy Alberto Varon G.
"""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import redis
import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# 1. Configuración por variables de entorno
# ---------------------------------------------------------------------------

def _parse_instancias(valor: str) -> dict:
    """Convierte 'instancia-1=http://host:5000,instancia-2=http://...' en un dict."""
    instancias = {}
    for parte in valor.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "=" not in parte:
            raise ValueError(
                f"Formato inválido en INSTANCIAS: '{parte}'. Se espera id=url_base"
            )
        instancia_id, url_base = parte.split("=", 1)
        instancias[instancia_id.strip()] = url_base.strip().rstrip("/")
    if not instancias:
        raise ValueError("La variable INSTANCIAS no contiene ninguna instancia válida")
    return instancias


INSTANCIAS = _parse_instancias(
    os.getenv(
        "INSTANCIAS",
        "instancia-1=http://ms-cotizacion-1:5000,"
        "instancia-2=http://ms-cotizacion-2:5000,"
        "instancia-3=http://ms-cotizacion-3:5000",
    )
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
INTERVALO_CICLO_SEGUNDOS = float(os.getenv("INTERVALO_CICLO_SEGUNDOS", "5"))
TIMEOUT_PROBE_SEGUNDOS = float(os.getenv("TIMEOUT_PROBE_SEGUNDOS", "3"))
CICLOS_FALLIDOS_PARA_NO_SALUDABLE = int(
    os.getenv("CICLOS_FALLIDOS_PARA_NO_SALUDABLE", "2")
)
CICLOS_EXITOSOS_PARA_REINTEGRAR = int(os.getenv("CICLOS_EXITOSOS_PARA_REINTEGRAR", "3"))
ARCHIVO_LOG = os.getenv("ARCHIVO_LOG", "/resultados/ms-monitor.jsonl")
PUERTO_DEBUG = int(os.getenv("PUERTO_DEBUG", "6000"))

# Contrato de negocio compartido con el resto de componentes.
# Payload sintético fijo y prima esperada: NO deben cambiarse, el harness usa el mismo.
PAYLOAD_SINTETICO = {
    "cliente_id": "SINTETICO-CANARY",
    "producto": "viaje",
    "monto_asegurado": 1000000,
}
PRIMA_ESPERADA = 15000.0
TOLERANCIA_PRIMA = 0.01

PREFIJO_CLAVE_REDIS = "solventa:health:"
COMPONENTE = "ms-monitor"

ESTADO_HEALTHY = "healthy"
ESTADO_UNHEALTHY = "unhealthy"


# ---------------------------------------------------------------------------
# 2. Registro de eventos (JSON Lines)
# ---------------------------------------------------------------------------

_lock_log = threading.Lock()


def _ahora_iso() -> str:
    """Timestamp UTC ISO-8601 con precisión de microsegundos."""
    return datetime.now(timezone.utc).isoformat()


def registrar_evento(evento: str, **campos) -> None:
    """
    Escribe un evento en formato JSON Lines. Serializa el acceso al archivo con un
    lock, porque los chequeos corren en paralelo en varios hilos y podrían intercalar
    escrituras parciales, corrompiendo el archivo que después lee el script de análisis.
    """
    registro = {
        "timestamp": _ahora_iso(),
        "componente": COMPONENTE,
        "evento": evento,
    }
    registro.update(campos)
    linea = json.dumps(registro, ensure_ascii=False)
    with _lock_log:
        try:
            os.makedirs(os.path.dirname(ARCHIVO_LOG), exist_ok=True)
            with open(ARCHIVO_LOG, "a", encoding="utf-8") as fh:
                fh.write(linea + "\n")
                fh.flush()
        except OSError as exc:
            # Si no se puede escribir el log, no se detiene el Monitor: se avisa por stderr.
            print(f"[ms-monitor] ERROR escribiendo log: {exc}", file=sys.stderr, flush=True)
    print(f"[ms-monitor] {linea}", flush=True)


# ---------------------------------------------------------------------------
# 3. Conexión a Redis con reintentos
# ---------------------------------------------------------------------------

def conectar_redis(intentos_maximos: int = 30, espera_segundos: float = 1.0):
    """
    Conecta a Redis reintentando. Docker Compose puede arrancar el Monitor antes de que
    Redis termine de estar listo; aunque el compose usa depends_on + healthcheck, el
    Monitor debe ser tolerante igualmente como buena práctica.
    """
    for intento in range(1, intentos_maximos + 1):
        try:
            cliente = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            cliente.ping()
            return cliente
        except redis.RedisError as exc:
            print(
                f"[ms-monitor] Redis no disponible (intento {intento}/{intentos_maximos}): {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(espera_segundos)
    raise RuntimeError(
        f"No fue posible conectar a Redis en {REDIS_HOST}:{REDIS_PORT} "
        f"después de {intentos_maximos} intentos"
    )


# ---------------------------------------------------------------------------
# 4. Máquina de estados en memoria
# ---------------------------------------------------------------------------
# Los contadores viven SOLO en memoria del proceso, por decisión explícita de diseño:
# ningún otro componente los necesita, solo el resultado final healthy/unhealthy le
# importa al Router.

estado_instancias = {
    instancia_id: {
        "estado_actual": ESTADO_HEALTHY,
        "contador_fallos_consecutivos": 0,
        "contador_exitos_consecutivos": 0,
    }
    for instancia_id in INSTANCIAS
}

_lock_estado = threading.Lock()


# ---------------------------------------------------------------------------
# 5. Chequeos individuales
# ---------------------------------------------------------------------------

def chequeo_vida(instancia_id: str, url_base: str, numero_ciclo: int) -> bool:
    """
    Ping/Echo: GET /health. Exitoso solo si responde HTTP 200.
    Cualquier excepción (timeout, conexión rechazada) se trata como FALLO del chequeo,
    nunca como error del propio Monitor.
    """
    inicio = time.perf_counter()
    resultado, detalle = "fallo", "desconocido"
    try:
        respuesta = requests.get(
            f"{url_base}/health", timeout=TIMEOUT_PROBE_SEGUNDOS
        )
        if respuesta.status_code == 200:
            resultado, detalle = "ok", "ok"
        else:
            detalle = f"http {respuesta.status_code}"
    except requests.exceptions.Timeout:
        detalle = "timeout"
    except requests.exceptions.ConnectionError:
        detalle = "error de conexion"
    except requests.exceptions.RequestException as exc:
        detalle = f"error de peticion: {exc.__class__.__name__}"

    latencia_ms = round((time.perf_counter() - inicio) * 1000, 3)
    registrar_evento(
        "resultado_probe",
        instancia_id=instancia_id,
        numero_ciclo=numero_ciclo,
        tipo_probe="vida",
        resultado=resultado,
        detalle=detalle,
        latencia_ms=latencia_ms,
    )
    return resultado == "ok"


def chequeo_semantico(instancia_id: str, url_base: str, numero_ciclo: int) -> bool:
    """
    Chequeo semántico: POST /cotizar con el payload sintético fijo. Exitoso solo si
    responde HTTP 200 Y la prima es 15000.0 (con tolerancia de 0.01 para evitar falsos
    negativos por representación de punto flotante).

    Este chequeo es el que detecta la falla 'prima_inconsistente', que el Ping/Echo NO
    puede detectar por sí solo (GET /health sigue respondiendo 200): esa es precisamente
    la razón por la que el diseño exige ambos chequeos siempre juntos.
    """
    inicio = time.perf_counter()
    resultado, detalle = "fallo", "desconocido"
    try:
        respuesta = requests.post(
            f"{url_base}/cotizar",
            json=PAYLOAD_SINTETICO,
            timeout=TIMEOUT_PROBE_SEGUNDOS,
        )
        if respuesta.status_code != 200:
            detalle = f"http {respuesta.status_code}"
        else:
            try:
                cuerpo = respuesta.json()
            except ValueError:
                detalle = "respuesta no es JSON valido"
            else:
                prima = cuerpo.get("prima")
                if prima is None:
                    detalle = "respuesta sin campo prima"
                elif not isinstance(prima, (int, float)):
                    detalle = f"prima no numerica: {prima!r}"
                elif abs(float(prima) - PRIMA_ESPERADA) <= TOLERANCIA_PRIMA:
                    resultado, detalle = "ok", "ok"
                else:
                    detalle = (
                        f"prima recibida {float(prima)}, esperada {PRIMA_ESPERADA}"
                    )
    except requests.exceptions.Timeout:
        detalle = "timeout"
    except requests.exceptions.ConnectionError:
        detalle = "error de conexion"
    except requests.exceptions.RequestException as exc:
        detalle = f"error de peticion: {exc.__class__.__name__}"

    latencia_ms = round((time.perf_counter() - inicio) * 1000, 3)
    registrar_evento(
        "resultado_probe",
        instancia_id=instancia_id,
        numero_ciclo=numero_ciclo,
        tipo_probe="semantico",
        resultado=resultado,
        detalle=detalle,
        latencia_ms=latencia_ms,
    )
    return resultado == "ok"


# ---------------------------------------------------------------------------
# 6. Evaluación de una instancia y actualización de estado
# ---------------------------------------------------------------------------

def escribir_estado_redis(cliente_redis, instancia_id: str, estado: str, numero_ciclo: int) -> None:
    """
    Persiste el estado en el Registro de Salud. Un fallo de escritura se registra como
    evento y NO detiene el ciclo del Monitor: el siguiente ciclo reintentará con
    normalidad.
    """
    clave = f"{PREFIJO_CLAVE_REDIS}{instancia_id}"
    try:
        cliente_redis.set(clave, estado)
        registrar_evento(
            "escritura_redis",
            instancia_id=instancia_id,
            numero_ciclo=numero_ciclo,
            estado_escrito=estado,
        )
    except redis.RedisError as exc:
        registrar_evento(
            "error_escritura_redis",
            instancia_id=instancia_id,
            numero_ciclo=numero_ciclo,
            detalle=f"{exc.__class__.__name__}: {exc}",
        )


def evaluar_instancia(cliente_redis, instancia_id: str, url_base: str, numero_ciclo: int) -> None:
    """
    Ejecuta AMBOS chequeos de una instancia en paralelo entre sí, aplica la máquina de
    estados y, si hay cambio de estado, escribe en Redis inmediatamente.

    Regla de evaluación: el resultado del ciclo es fallido si CUALQUIERA de los dos
    chequeos falló; es exitoso solo si AMBOS fueron exitosos. Los dos chequeos no son
    alternativas, operan siempre juntos.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        futuro_vida = executor.submit(chequeo_vida, instancia_id, url_base, numero_ciclo)
        futuro_semantico = executor.submit(
            chequeo_semantico, instancia_id, url_base, numero_ciclo
        )
        vida_ok = futuro_vida.result()
        semantico_ok = futuro_semantico.result()

    ciclo_exitoso = vida_ok and semantico_ok

    with _lock_estado:
        estado = estado_instancias[instancia_id]
        estado_anterior = estado["estado_actual"]
        cambio = None

        if ciclo_exitoso:
            estado["contador_exitos_consecutivos"] += 1
            estado["contador_fallos_consecutivos"] = 0
            if (
                estado_anterior == ESTADO_UNHEALTHY
                and estado["contador_exitos_consecutivos"] >= CICLOS_EXITOSOS_PARA_REINTEGRAR
            ):
                estado["estado_actual"] = ESTADO_HEALTHY
                cambio = ("exitos_consecutivos", ESTADO_HEALTHY)
        else:
            estado["contador_fallos_consecutivos"] += 1
            estado["contador_exitos_consecutivos"] = 0
            if (
                estado_anterior == ESTADO_HEALTHY
                and estado["contador_fallos_consecutivos"] >= CICLOS_FALLIDOS_PARA_NO_SALUDABLE
            ):
                estado["estado_actual"] = ESTADO_UNHEALTHY
                cambio = ("fallos_consecutivos", ESTADO_UNHEALTHY)

    if cambio is not None:
        motivo, estado_nuevo = cambio
        # 'cambio_estado' es el evento que el script de análisis usa como instante de
        # detección / reintegración: es el evento más importante del experimento.
        registrar_evento(
            "cambio_estado",
            instancia_id=instancia_id,
            numero_ciclo=numero_ciclo,
            estado_anterior=estado_anterior,
            estado_nuevo=estado_nuevo,
            motivo=motivo,
        )
        # La escritura ocurre inmediatamente después de decidir el cambio (no al final
        # del ciclo completo), para minimizar el desfase entre detección y disponibilidad
        # del nuevo estado para el Router.
        escribir_estado_redis(cliente_redis, instancia_id, estado_nuevo, numero_ciclo)


# ---------------------------------------------------------------------------
# 7. Bucle principal del Monitor
# ---------------------------------------------------------------------------

def bucle_monitor(cliente_redis) -> None:
    """
    Ciclo indefinido cada INTERVALO_CICLO_SEGUNDOS. Las tres instancias se verifican
    de forma CONCURRENTE, de modo que el tiempo total del ciclo sea
    ~TIMEOUT_PROBE_SEGUNDOS en el peor caso.
    """
    numero_ciclo = 0
    with ThreadPoolExecutor(max_workers=len(INSTANCIAS)) as executor:
        while True:
            inicio_ciclo = time.perf_counter()
            numero_ciclo += 1
            registrar_evento("ciclo_iniciado", numero_ciclo=numero_ciclo)

            futuros = [
                executor.submit(
                    evaluar_instancia, cliente_redis, instancia_id, url_base, numero_ciclo
                )
                for instancia_id, url_base in INSTANCIAS.items()
            ]
            for futuro in futuros:
                try:
                    futuro.result()
                except Exception as exc:  # red de seguridad: el Monitor nunca debe caerse
                    registrar_evento(
                        "error_ciclo",
                        numero_ciclo=numero_ciclo,
                        detalle=f"{exc.__class__.__name__}: {exc}",
                    )

            # Espera el resto del intervalo, descontando lo que tardó el ciclo, para que
            # los ciclos ocurran cada 5 s reales y no cada 5 s + duración del ciclo.
            transcurrido = time.perf_counter() - inicio_ciclo
            time.sleep(max(0.0, INTERVALO_CICLO_SEGUNDOS - transcurrido))


# ---------------------------------------------------------------------------
# 8. Endpoint opcional de depuración
# ---------------------------------------------------------------------------
# NO forma parte del protocolo de medición. Los datos oficiales para el análisis salen
# exclusivamente del archivo JSON Lines. Este endpoint es solo ayuda manual del equipo
# y sirve además como healthcheck de Docker Compose.

app = Flask(__name__)


@app.get("/status")
def status():
    with _lock_estado:
        respuesta = {
            instancia_id: {
                "estado": datos["estado_actual"],
                "fallos_consecutivos": datos["contador_fallos_consecutivos"],
                "exitos_consecutivos": datos["contador_exitos_consecutivos"],
            }
            for instancia_id, datos in estado_instancias.items()
        }
    return jsonify(respuesta), 200


# ---------------------------------------------------------------------------
# 9. Arranque
# ---------------------------------------------------------------------------

def main() -> None:
    cliente_redis = conectar_redis()

    # Inicializar las tres claves en "healthy" ANTES de registrar el evento de inicio
    # y antes de que el Router empiece a atender tráfico.
    for instancia_id in INSTANCIAS:
        escribir_estado_redis(cliente_redis, instancia_id, ESTADO_HEALTHY, numero_ciclo=0)

    registrar_evento(
        "inicio",
        instancias=list(INSTANCIAS.keys()),
        intervalo_ciclo_segundos=INTERVALO_CICLO_SEGUNDOS,
    )

    # El bucle de monitoreo corre en un hilo daemon; Flask queda en el hilo principal
    # sirviendo únicamente /status. Si el equipo prefiere no usar Flask, basta con
    # llamar bucle_monitor(cliente_redis) directamente y omitir app.run.
    hilo = threading.Thread(
        target=bucle_monitor, args=(cliente_redis,), name="bucle-monitor", daemon=True
    )
    hilo.start()

    app.run(host="0.0.0.0", port=PUERTO_DEBUG, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
