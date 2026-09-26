"""
generador_ataque.py
=====================
Inyecta, de forma puntual y controlada, uno de los dos patrones de ataque
que el experimento necesita poder reproducir a voluntad:

- violacion_alcance: un actor 'cliente' consultando un perfil que no es el
  suyo, en una única solicitud.
- rafaga_diversidad: un actor de rol ampliado ('asesor' u 'operaciones')
  consultando muchos clientes distintos concentrados en pocos segundos.

Cada función devuelve la lista de registros de las solicitudes que envió,
en el mismo formato que usa generador_trafico_legitimo.py, para que
ejecutar_combinacion.py pueda combinarlos con el resto del registro local de
la corrida. También se puede invocar este script de forma independiente
(ver 'Uso' más abajo) para inyectar un ataque suelto contra un entorno ya
integrado, sin necesidad de orquestar una corrida completa.
"""

import argparse
import logging
import random
import time

import requests

from comun import RegistradorSolicitudes, actores_por_rol, cargar_directorio_actores, consultar_perfil

logging.basicConfig(level=logging.INFO, format="%(asctime)s generador-ataque %(levelname)s %(message)s")
logger = logging.getLogger("generador-ataque")

# Espera entre solicitudes sucesivas de la ráfaga de diversidad: deliberadamente
# corta, para que el patrón quede concentrado en pocos segundos y bien
# adentro de cualquier ventana de detección razonable.
ESPERA_ENTRE_SOLICITUDES_RAFAGA_SEGUNDOS = (0.1, 0.5)

# Valor de respaldo si no se puede leer el umbral vigente desde el monitor
# (por ejemplo, porque no se le pasó --url-monitor). Se eligió por encima de
# cualquier umbral de diversidad razonable para un experimento de este
# tamaño, de forma que el patrón dispare la detección incluso sin
# autocalibración.
NUM_CLIENTES_RAFAGA_POR_DEFECTO = 15
MARGEN_RAFAGA_SOBRE_UMBRAL = 5


def _obtener_umbral_diversidad_vigente(url_monitor: str, timeout_segundos: float = 3):
    """
    Consulta el endpoint de diagnóstico del monitor para leer el umbral de
    diversidad realmente activo en este momento, en vez de asumir un valor
    fijo. Así la ráfaga queda calibrada por encima del umbral vigente aunque
    se esté en medio de una corrida de calibración que lo cambia entre
    repeticiones.

    Si el monitor no responde, se devuelve None y quien llama decide un
    valor por defecto: es una consulta de conveniencia, no debe bloquear la
    generación del ataque.
    """
    try:
        respuesta = requests.get(
            f"{url_monitor}/estado", params={"actor_id": "harness-calibracion"}, timeout=timeout_segundos
        )
        respuesta.raise_for_status()
        return respuesta.json().get("umbral_diversidad")
    except requests.exceptions.RequestException as exc:
        logger.warning("no_se_pudo_leer_umbral_diversidad error=%s", exc)
        return None


def ejecutar_violacion_alcance(url_gateway, directorio, cantidad_perfiles, registrador, actor_id=None):
    ids_clientes = actores_por_rol(directorio, "cliente")
    if not ids_clientes:
        raise ValueError("El directorio de actores no tiene ningún actor con rol 'cliente'")
    actor_id = actor_id or random.choice(ids_clientes)
    client_id_propio = directorio.get(actor_id, {}).get("client_id_propio")
    if not client_id_propio:
        raise ValueError(f"El actor '{actor_id}' no tiene client_id_propio configurado")

    pool_ajeno = [f"cli-{i:03d}" for i in range(1, cantidad_perfiles + 1) if f"cli-{i:03d}" != client_id_propio]
    client_id_ajeno = random.choice(pool_ajeno)

    logger.info(
        "inyectando_violacion_alcance actor_id=%s client_id_propio=%s client_id_consultado=%s",
        actor_id, client_id_propio, client_id_ajeno,
    )

    registro = consultar_perfil(url_gateway, actor_id, client_id_ajeno)
    registro.update(es_trafico_legitimo=False, es_ataque_inyectado=True, patron_ataque="violacion_alcance")
    if registrador is not None:
        registrador.registrar(registro)
    return [registro]


def ejecutar_rafaga_diversidad(url_gateway, directorio, cantidad_perfiles, registrador, actor_id=None,
                                rol_preferido="asesor", num_clientes=None, url_monitor=None):
    candidatos = actores_por_rol(directorio, rol_preferido) or actores_por_rol(directorio, "operaciones")
    if not candidatos:
        raise ValueError("El directorio de actores no tiene ningún actor de rol ampliado disponible")
    actor_id = actor_id or random.choice(candidatos)

    if num_clientes is None:
        umbral_vigente = _obtener_umbral_diversidad_vigente(url_monitor) if url_monitor else None
        num_clientes = (
            umbral_vigente + MARGEN_RAFAGA_SOBRE_UMBRAL
            if umbral_vigente is not None
            else NUM_CLIENTES_RAFAGA_POR_DEFECTO
        )

    pool = [f"cli-{i:03d}" for i in range(1, cantidad_perfiles + 1)]
    clientes_objetivo = random.sample(pool, k=min(num_clientes, len(pool)))

    logger.info("inyectando_rafaga_diversidad actor_id=%s num_clientes=%d", actor_id, len(clientes_objetivo))

    registros = []
    for client_id in clientes_objetivo:
        registro = consultar_perfil(url_gateway, actor_id, client_id)
        registro.update(es_trafico_legitimo=False, es_ataque_inyectado=True, patron_ataque="rafaga_diversidad")
        if registrador is not None:
            registrador.registrar(registro)
        registros.append(registro)
        time.sleep(random.uniform(*ESPERA_ENTRE_SOLICITUDES_RAFAGA_SEGUNDOS))
    return registros


def main():
    parser = argparse.ArgumentParser(description="Inyecta un patrón de ataque puntual contra api-gateway.")
    parser.add_argument("--patron", choices=("violacion_alcance", "rafaga_diversidad"), required=True)
    parser.add_argument("--actor-id", default=None, help="Actor a usar; si se omite, se elige uno al azar del rol adecuado.")
    parser.add_argument("--num-clientes-rafaga", type=int, default=None,
                         help="Solo aplica a rafaga_diversidad; si se omite, se autocalibra contra --url-monitor.")
    parser.add_argument("--ruta-directorio-actores", default="../api-gateway/config/actores.json")
    parser.add_argument("--cantidad-perfiles", type=int, default=50)
    parser.add_argument("--url-gateway", default="http://localhost:8000")
    parser.add_argument("--url-monitor", default="http://localhost:8002")
    parser.add_argument("--salida", default="resultados/ataque.jsonl")
    args = parser.parse_args()

    directorio = cargar_directorio_actores(args.ruta_directorio_actores)
    registrador = RegistradorSolicitudes(args.salida)

    if args.patron == "violacion_alcance":
        ejecutar_violacion_alcance(args.url_gateway, directorio, args.cantidad_perfiles, registrador, args.actor_id)
    else:
        ejecutar_rafaga_diversidad(
            args.url_gateway, directorio, args.cantidad_perfiles, registrador, args.actor_id,
            num_clientes=args.num_clientes_rafaga, url_monitor=args.url_monitor,
        )


if __name__ == "__main__":
    main()
