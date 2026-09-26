"""
ejecutar_combinacion.py
=========================
Orquesta una corrida completa de una combinación de prueba: lanza el tráfico
legítimo de fondo, inyecta el patrón de ataque correspondiente (si la
combinación lo requiere) en un instante conocido dentro de la corrida, y al
terminar recupera de registro-auditoria todo lo que el sistema detectó y
reaccionó durante esa ventana de tiempo.

Las combinaciones en sí (duración, mezcla de roles, qué ataque inyectar y
en qué momento) se definen en combinaciones.json, no en este script: así se
pueden agregar o ajustar combinaciones sin tocar código.

El archivo de salida es un único JSON Lines con tres tipos de línea,
distinguibles por el campo "tipo_linea":
  - "metadata": una única línea al inicio, con los parámetros usados y los
    timestamps de inicio/fin de la corrida.
  - "solicitud": una línea por cada solicitud que el propio harness envió
    (tráfico legítimo y ataque inyectado), en el mismo formato que producen
    generador_trafico_legitimo.py y generador_ataque.py.
  - "evento_auditoria": una línea por cada registro que devolvió
    registro-auditoria al consultarlo al final de la corrida.

analizar_resultados.py lee este mismo archivo para calcular las métricas,
sin necesidad de volver a consultar ningún servicio en vivo.

Uso:
    python ejecutar_combinacion.py --combinacion 2 --repeticion 7 --salida resultados/comb2_rep7.jsonl
"""

import argparse
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import redis
import requests

from comun import RegistradorSolicitudes, actores_por_rol, cargar_directorio_actores, formatear_timestamp
from generador_ataque import ejecutar_rafaga_diversidad, ejecutar_violacion_alcance
from generador_trafico_legitimo import generar_trafico

logging.basicConfig(level=logging.INFO, format="%(asctime)s ejecutar-combinacion %(levelname)s %(message)s")
logger = logging.getLogger("ejecutar-combinacion")

CANAL_HALLAZGOS = "canal:hallazgos"

# Tiempo máximo que se espera, tras inyectar el ataque, a que aparezca en
# canal:hallazgos el hallazgo que se va a duplicar. Si nada llega en ese
# margen (por ejemplo porque el ataque no fue detectado), se continúa sin
# simular el duplicado en vez de bloquear el resto de la corrida.
ESPERA_MAXIMA_HALLAZGO_PARA_DUPLICAR_SEGUNDOS = 15


def _esperar_y_duplicar_hallazgo(cliente_redis, actor_id_filtro):
    """
    Se suscribe a canal:hallazgos ANTES de que se inyecte el ataque (se
    lanza en su propio hilo justo antes de la inyección), espera el primer
    hallazgo que corresponda al actor atacado, y lo vuelve a publicar tal
    cual una segunda vez.

    Esto reproduce, de forma controlada, el escenario de una entrega
    duplicada del mismo evento_id por el bus de mensajería, que es lo que la
    deduplicación de orquestador-reaccion debe manejar sin ejecutar la
    reacción de bloqueo dos veces. Republicar el mensaje capturado tal cual
    (en vez de fabricar uno nuevo) garantiza que el evento_id coincide
    exactamente con el original, que es la condición que dispara la
    deduplicación.
    """
    pubsub = cliente_redis.pubsub()
    pubsub.subscribe(CANAL_HALLAZGOS)
    limite = time.monotonic() + ESPERA_MAXIMA_HALLAZGO_PARA_DUPLICAR_SEGUNDOS
    try:
        while time.monotonic() < limite:
            mensaje = pubsub.get_message(timeout=1.0)
            if mensaje is None or mensaje.get("type") != "message":
                continue
            try:
                hallazgo = json.loads(mensaje["data"])
            except json.JSONDecodeError:
                continue
            if hallazgo.get("actor_id") == actor_id_filtro:
                cliente_redis.publish(CANAL_HALLAZGOS, mensaje["data"])
                logger.info(
                    "hallazgo_duplicado_publicado evento_id=%s actor_id=%s",
                    hallazgo.get("evento_id"), actor_id_filtro,
                )
                return
    finally:
        pubsub.close()
    logger.warning("no_se_recibio_hallazgo_para_duplicar actor_id=%s", actor_id_filtro)


def _actor_por_defecto(directorio, patron_ataque):
    if patron_ataque == "violacion_alcance":
        candidatos = actores_por_rol(directorio, "cliente")
    else:
        candidatos = actores_por_rol(directorio, "asesor") or actores_por_rol(directorio, "operaciones")
    return candidatos[0] if candidatos else None


def _consultar_eventos_auditoria(url_registro_auditoria, desde, hasta):
    try:
        respuesta = requests.get(
            f"{url_registro_auditoria}/eventos", params={"desde": desde, "hasta": hasta}, timeout=15
        )
        respuesta.raise_for_status()
        return respuesta.json()
    except requests.exceptions.RequestException as exc:
        logger.error("fallo_consulta_registro_auditoria error=%s", exc)
        return []


def _combinar_salida(ruta_salida, ruta_solicitudes_temporal, eventos_auditoria,
                      combinacion_id, repeticion, config, timestamp_inicio, timestamp_fin, umbral_bajo_prueba):
    os.makedirs(os.path.dirname(os.path.abspath(ruta_salida)) or ".", exist_ok=True)

    num_solicitudes = 0
    with open(ruta_salida, "w", encoding="utf-8") as salida:
        metadata = {
            "tipo_linea": "metadata",
            "combinacion": combinacion_id,
            "repeticion": repeticion,
            "parametros": config,
            "umbral_bajo_prueba": umbral_bajo_prueba,
            "timestamp_inicio_corrida": timestamp_inicio,
            "timestamp_fin_corrida": timestamp_fin,
        }
        salida.write(json.dumps(metadata, ensure_ascii=False) + "\n")

        if os.path.exists(ruta_solicitudes_temporal):
            with open(ruta_solicitudes_temporal, "r", encoding="utf-8") as temporal:
                for linea in temporal:
                    linea = linea.strip()
                    if not linea:
                        continue
                    registro = json.loads(linea)
                    registro["tipo_linea"] = "solicitud"
                    salida.write(json.dumps(registro, ensure_ascii=False) + "\n")
                    num_solicitudes += 1

        for evento in eventos_auditoria:
            evento = dict(evento)
            evento["tipo_linea"] = "evento_auditoria"
            salida.write(json.dumps(evento, ensure_ascii=False) + "\n")

    return num_solicitudes, len(eventos_auditoria)


def ejecutar(combinacion_id, repeticion, config, args):
    directorio = cargar_directorio_actores(args.ruta_directorio_actores)
    ruta_solicitudes_temporal = f"{args.salida}.solicitudes.tmp.jsonl"
    if os.path.exists(ruta_solicitudes_temporal):
        os.remove(ruta_solicitudes_temporal)
    registrador = RegistradorSolicitudes(ruta_solicitudes_temporal)

    timestamp_inicio = formatear_timestamp(datetime.now(timezone.utc))
    logger.info("inicio_corrida combinacion=%s repeticion=%s timestamp=%s",
                combinacion_id, repeticion, timestamp_inicio)

    hilo_trafico = threading.Thread(
        target=generar_trafico,
        kwargs=dict(
            duracion_segundos=config["duracion_segundos"],
            tasa_promedio_por_segundo=config["tasa_promedio_por_segundo"],
            proporcion_clientes=config["proporcion_clientes"],
            proporcion_asesores=config["proporcion_asesores"],
            proporcion_operaciones=config["proporcion_operaciones"],
            ruta_directorio_actores=args.ruta_directorio_actores,
            cantidad_perfiles=args.cantidad_perfiles,
            url_gateway=args.url_gateway,
            ventana_referencia_segundos=args.ventana_referencia_segundos,
            salida=ruta_solicitudes_temporal,
        ),
        daemon=True,
    )
    hilo_trafico.start()

    patron_ataque = config.get("patron_ataque")
    if patron_ataque:
        fraccion = config.get("instante_inyeccion_fraccion", 0.5)
        time.sleep(config["duracion_segundos"] * fraccion)

        actor_ataque = args.actor_ataque or _actor_por_defecto(directorio, patron_ataque)
        if actor_ataque is None:
            raise ValueError("No hay ningún actor disponible en el directorio para el patrón de ataque configurado")

        hilo_duplicador = None
        if config.get("simular_entrega_duplicada"):
            cliente_redis = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
            hilo_duplicador = threading.Thread(
                target=_esperar_y_duplicar_hallazgo,
                args=(cliente_redis, actor_ataque),
                daemon=True,
            )
            hilo_duplicador.start()
            # Pequeña espera para que la suscripción quede activa antes de
            # disparar el ataque: si el hallazgo original se publicara antes
            # de que este hilo esté escuchando, se perdería y no habría nada
            # que duplicar.
            time.sleep(0.5)

        if patron_ataque == "violacion_alcance":
            ejecutar_violacion_alcance(args.url_gateway, directorio, args.cantidad_perfiles, registrador, actor_ataque)
        elif patron_ataque == "rafaga_diversidad":
            ejecutar_rafaga_diversidad(
                args.url_gateway, directorio, args.cantidad_perfiles, registrador, actor_ataque,
                url_monitor=args.url_monitor,
            )
        else:
            raise ValueError(f"patron_ataque desconocido en la combinación: {patron_ataque}")

        if hilo_duplicador is not None:
            hilo_duplicador.join(timeout=ESPERA_MAXIMA_HALLAZGO_PARA_DUPLICAR_SEGUNDOS + 5)

    hilo_trafico.join(timeout=config["duracion_segundos"] + 60)

    logger.info("esperando_margen_final segundos=%s", args.margen_espera_final_segundos)
    time.sleep(args.margen_espera_final_segundos)

    timestamp_fin = formatear_timestamp(datetime.now(timezone.utc))

    eventos_auditoria = _consultar_eventos_auditoria(args.url_registro_auditoria, timestamp_inicio, timestamp_fin)

    num_solicitudes, num_eventos = _combinar_salida(
        args.salida, ruta_solicitudes_temporal, eventos_auditoria,
        combinacion_id, repeticion, config, timestamp_inicio, timestamp_fin, args.umbral_bajo_prueba,
    )
    os.remove(ruta_solicitudes_temporal)

    logger.info(
        "corrida_finalizada combinacion=%s repeticion=%s salida=%s solicitudes=%d eventos_auditoria=%d",
        combinacion_id, repeticion, args.salida, num_solicitudes, num_eventos,
    )


def main():
    parser = argparse.ArgumentParser(description="Ejecuta una corrida completa de una combinación de prueba del experimento.")
    parser.add_argument("--combinacion", required=True)
    parser.add_argument("--repeticion", required=True)
    parser.add_argument("--salida", required=True)
    parser.add_argument("--config-combinaciones", default="combinaciones.json")
    parser.add_argument("--ruta-directorio-actores", default="../api-gateway/config/actores.json")
    parser.add_argument("--cantidad-perfiles", type=int, default=50)
    parser.add_argument("--url-gateway", default="http://localhost:8000")
    parser.add_argument("--url-monitor", default="http://localhost:8002")
    parser.add_argument("--url-registro-auditoria", default="http://localhost:8004")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--ventana-referencia-segundos", type=float, default=30)
    parser.add_argument("--margen-espera-final-segundos", type=float, default=5)
    parser.add_argument("--actor-ataque", default=None,
                         help="Actor a usar para el ataque de esta combinación; por defecto se elige uno fijo "
                              "del directorio, para que las corridas sean reproducibles.")
    parser.add_argument("--umbral-bajo-prueba", type=int, default=None,
                         help="Umbral (UMBRAL_VOLUMEN o UMBRAL_DIVERSIDAD) activo en monitor-accesos-indebidos "
                              "durante esta corrida. Es solo un dato para el análisis posterior: este script no "
                              "configura el monitor, así que hay que decírselo si se está calibrando umbrales.")
    args = parser.parse_args()

    with open(args.config_combinaciones, "r", encoding="utf-8") as f:
        combinaciones = json.load(f)
    if args.combinacion not in combinaciones:
        raise SystemExit(f"La combinación '{args.combinacion}' no existe en {args.config_combinaciones}")
    config = combinaciones[args.combinacion]

    ejecutar(args.combinacion, args.repeticion, config, args)


if __name__ == "__main__":
    main()
