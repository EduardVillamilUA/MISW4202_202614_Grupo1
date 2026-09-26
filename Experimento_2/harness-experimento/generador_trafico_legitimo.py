"""
generador_trafico_legitimo.py
==============================
Simula tráfico normal del sistema: actores 'cliente' consultando su propio
perfil de forma esporádica, y actores 'asesor'/'operaciones' consultando un
puñado de perfiles distintos por ventana de tiempo, dentro de lo que la
regla heurística de detección debería considerar un patrón inofensivo si los
umbrales están bien calibrados.

Cada actor participante corre en su propio hilo con su propio reloj: no hay
un único generador central emitiendo solicitudes a una tasa fija, porque el
comportamiento que se quiere imitar es el de varias personas usando el
sistema de forma independiente, no el de un solo proceso disparando
peticiones en ráfaga regular.
"""

import argparse
import logging
import random
import threading
import time

from comun import RegistradorSolicitudes, actores_por_rol, cargar_directorio_actores, consultar_perfil

logging.basicConfig(level=logging.INFO, format="%(asctime)s trafico-legitimo %(levelname)s %(message)s")
logger = logging.getLogger("trafico-legitimo")

# Rango recomendado de intervalo entre consultas de un mismo actor 'cliente':
# suficientemente espaciado para no parecer un script, suficientemente
# frecuente para generar volumen dentro de corridas de pocos minutos.
INTERVALO_CLIENTE_MIN_SEGUNDOS = 2
INTERVALO_CLIENTE_MAX_SEGUNDOS = 15

# Rango recomendado de clientes distintos que un actor de alcance ampliado
# consulta dentro de cada ventana de referencia.
DIVERSIDAD_MIN_POR_VENTANA = 2
DIVERSIDAD_MAX_POR_VENTANA = 4


def _worker_cliente(actor_id, client_id_propio, url_gateway, registrador, hasta_monotonic):
    while time.monotonic() < hasta_monotonic:
        time.sleep(random.uniform(INTERVALO_CLIENTE_MIN_SEGUNDOS, INTERVALO_CLIENTE_MAX_SEGUNDOS))
        if time.monotonic() >= hasta_monotonic:
            break
        registro = consultar_perfil(url_gateway, actor_id, client_id_propio)
        registro.update(es_trafico_legitimo=True, es_ataque_inyectado=False, patron_ataque=None)
        registrador.registrar(registro)


def _worker_alcance_ampliado(actor_id, pool_clientes, ventana_referencia_segundos, url_gateway,
                              registrador, hasta_monotonic):
    while time.monotonic() < hasta_monotonic:
        num_objetivo = random.randint(DIVERSIDAD_MIN_POR_VENTANA, DIVERSIDAD_MAX_POR_VENTANA)
        clientes_ciclo = random.sample(pool_clientes, k=min(num_objetivo, len(pool_clientes)))
        # Las consultas de este ciclo se reparten a lo largo de la ventana de
        # referencia, con algo de aleatoriedad, para no producirlas todas de
        # golpe (lo que sí parecería una ráfaga) ni tampoco a intervalos
        # artificialmente uniformes.
        intervalo_base = ventana_referencia_segundos / max(num_objetivo, 1)
        for client_id in clientes_ciclo:
            if time.monotonic() >= hasta_monotonic:
                return
            registro = consultar_perfil(url_gateway, actor_id, client_id)
            registro.update(es_trafico_legitimo=True, es_ataque_inyectado=False, patron_ataque=None)
            registrador.registrar(registro)
            time.sleep(random.uniform(intervalo_base * 0.5, intervalo_base * 1.5))


def _seleccionar_subconjunto(ids, proporcion):
    """
    Elige, de entre los actores disponibles de un rol, cuáles participan en
    esta corrida en particular, en una cantidad proporcional a `proporcion`.
    Esto permite variar la mezcla de tráfico entre corridas sin tener que
    editar el directorio de actores cada vez.
    """
    if not ids or proporcion <= 0:
        return []
    cantidad = min(len(ids), max(1, round(len(ids) * proporcion)))
    return random.sample(ids, k=cantidad)


def generar_trafico(
    duracion_segundos, tasa_promedio_por_segundo, proporcion_clientes, proporcion_asesores,
    proporcion_operaciones, ruta_directorio_actores, cantidad_perfiles, url_gateway,
    ventana_referencia_segundos, salida,
):
    directorio = cargar_directorio_actores(ruta_directorio_actores)
    registrador = RegistradorSolicitudes(salida)

    activos_clientes = _seleccionar_subconjunto(actores_por_rol(directorio, "cliente"), proporcion_clientes)
    activos_asesores = _seleccionar_subconjunto(actores_por_rol(directorio, "asesor"), proporcion_asesores)
    activos_operaciones = _seleccionar_subconjunto(actores_por_rol(directorio, "operaciones"), proporcion_operaciones)
    pool_clientes = [f"cli-{i:03d}" for i in range(1, cantidad_perfiles + 1)]

    # tasa_promedio_por_segundo es, en esta implementación, un parámetro
    # informativo: la cadencia real de cada actor sigue los rangos por rol
    # definidos arriba, que ya están pensados para no disparar la regla
    # heurística cuando los umbrales están bien calibrados. Atarla además a
    # un control fino de la tasa global agregaría una segunda fuente de
    # verdad sobre el ritmo del tráfico sin aportar precisión adicional a lo
    # que el experimento necesita medir; en cambio, se usa para estimar y
    # loguear la tasa esperada, como referencia para quien opera la corrida.
    tasa_estimada = len(activos_clientes) / ((INTERVALO_CLIENTE_MIN_SEGUNDOS + INTERVALO_CLIENTE_MAX_SEGUNDOS) / 2)
    tasa_estimada += (len(activos_asesores) + len(activos_operaciones)) * (
        (DIVERSIDAD_MIN_POR_VENTANA + DIVERSIDAD_MAX_POR_VENTANA) / 2
    ) / ventana_referencia_segundos
    logger.info(
        "iniciando_trafico_legitimo actores_cliente=%d actores_asesor=%d actores_operaciones=%d "
        "tasa_objetivo=%.2f tasa_estimada=%.2f duracion_s=%d",
        len(activos_clientes), len(activos_asesores), len(activos_operaciones),
        tasa_promedio_por_segundo, tasa_estimada, duracion_segundos,
    )

    hasta_monotonic = time.monotonic() + duracion_segundos
    hilos = []

    for actor_id in activos_clientes:
        client_id_propio = directorio[actor_id].get("client_id_propio")
        if not client_id_propio:
            logger.warning("actor_cliente_sin_client_id_propio actor_id=%s (se omite)", actor_id)
            continue
        hilos.append(threading.Thread(
            target=_worker_cliente,
            args=(actor_id, client_id_propio, url_gateway, registrador, hasta_monotonic),
            daemon=True,
        ))

    for actor_id in activos_asesores + activos_operaciones:
        hilos.append(threading.Thread(
            target=_worker_alcance_ampliado,
            args=(actor_id, pool_clientes, ventana_referencia_segundos, url_gateway, registrador, hasta_monotonic),
            daemon=True,
        ))

    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=duracion_segundos + 30)

    logger.info("trafico_legitimo_finalizado salida=%s", salida)


def main():
    parser = argparse.ArgumentParser(description="Genera tráfico legítimo simulado contra api-gateway.")
    parser.add_argument("--duracion-segundos", type=int, default=60)
    parser.add_argument("--tasa-promedio-por-segundo", type=float, default=1.0)
    parser.add_argument("--proporcion-clientes", type=float, default=0.5)
    parser.add_argument("--proporcion-asesores", type=float, default=0.35)
    parser.add_argument("--proporcion-operaciones", type=float, default=0.15)
    parser.add_argument("--ruta-directorio-actores", default="../api-gateway/config/actores.json")
    parser.add_argument("--cantidad-perfiles", type=int, default=50)
    parser.add_argument("--url-gateway", default="http://localhost:8000")
    parser.add_argument("--ventana-referencia-segundos", type=float, default=30)
    parser.add_argument("--salida", default="resultados/trafico_legitimo.jsonl")
    args = parser.parse_args()

    generar_trafico(
        duracion_segundos=args.duracion_segundos,
        tasa_promedio_por_segundo=args.tasa_promedio_por_segundo,
        proporcion_clientes=args.proporcion_clientes,
        proporcion_asesores=args.proporcion_asesores,
        proporcion_operaciones=args.proporcion_operaciones,
        ruta_directorio_actores=args.ruta_directorio_actores,
        cantidad_perfiles=args.cantidad_perfiles,
        url_gateway=args.url_gateway,
        ventana_referencia_segundos=args.ventana_referencia_segundos,
        salida=args.salida,
    )


if __name__ == "__main__":
    main()
