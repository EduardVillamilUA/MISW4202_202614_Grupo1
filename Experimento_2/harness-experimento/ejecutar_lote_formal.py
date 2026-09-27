"""
ejecutar_lote_formal.py
=========================
Automatiza el lanzamiento secuencial de las repeticiones formales de las tres
combinaciones de prueba definidas en `combinaciones.json`. Reanuda desde la
última repetición ya presente en el directorio de salida en vez de repetir
desde cero, así que es seguro volver a invocarlo si una corrida anterior se
detuvo a la mitad.

Antes de cada repetición limpia el estado de Redis que depende de la corrida
anterior (el conjunto `bloqueados` y las ventanas deslizantes `ventana:*`),
para que cada repetición sea independiente: el actor que se usa para el
ataque queda disponible de nuevo (en vez de permanecer bloqueado por la
detección de la repetición previa) y ninguna repetición arrastra el conteo
de diversidad de la que la precedió.

Uso:
    python ejecutar_lote_formal.py --repeticiones 20 --umbral-bajo-prueba 8
"""

import argparse
import glob
import json
import logging
import os
import re

import redis

from ejecutar_combinacion import ejecutar

logging.basicConfig(level=logging.INFO, format="%(asctime)s ejecutar-lote INFO %(message)s")
logger = logging.getLogger("ejecutar-lote")


def _ultima_repeticion_presente(salida_dir: str, combinacion_id: str) -> int:
    patron = os.path.join(salida_dir, f"comb{combinacion_id}_rep*.jsonl")
    maximo = 0
    for ruta in glob.glob(patron):
        m = re.search(r"_rep(\d+)\.jsonl$", ruta)
        if m:
            maximo = max(maximo, int(m.group(1)))
    return maximo


def _limpiar_estado_redis(redis_host: str, redis_port: int) -> None:
    cliente = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    cliente.delete("bloqueados")
    claves_ventana = cliente.keys("ventana:*")
    if claves_ventana:
        cliente.delete(*claves_ventana)


def main():
    parser = argparse.ArgumentParser(
        description="Ejecuta secuencialmente las repeticiones formales que falten de cada combinación."
    )
    parser.add_argument("--combinaciones", default="1,2,3", help="Lista separada por comas de IDs de combinación.")
    parser.add_argument("--repeticiones", type=int, default=20, help="Número total de repeticiones por combinación.")
    parser.add_argument("--config-combinaciones", default="combinaciones.json")
    parser.add_argument("--salida-dir", default="resultados")
    parser.add_argument("--ruta-directorio-actores", default="../api-gateway/config/actores.json")
    parser.add_argument("--cantidad-perfiles", type=int, default=50)
    parser.add_argument("--url-gateway", default="http://localhost:8000")
    parser.add_argument("--url-monitor", default="http://localhost:8002")
    parser.add_argument("--url-registro-auditoria", default="http://localhost:8004")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--ventana-referencia-segundos", type=float, default=30)
    parser.add_argument("--margen-espera-final-segundos", type=float, default=5)
    parser.add_argument(
        "--actor-ataque", default=None,
        help="Fijo por defecto (el primero disponible del rol adecuado) para que las repeticiones sean "
             "comparables entre sí; se limpia bloqueados antes de cada una para que pueda reutilizarse.",
    )
    parser.add_argument("--umbral-bajo-prueba", type=int, default=None)
    args = parser.parse_args()

    with open(args.config_combinaciones, "r", encoding="utf-8") as f:
        combinaciones = json.load(f)

    os.makedirs(args.salida_dir, exist_ok=True)

    for combinacion_id in (c.strip() for c in args.combinaciones.split(",")):
        if combinacion_id not in combinaciones:
            raise SystemExit(f"La combinación '{combinacion_id}' no existe en {args.config_combinaciones}")
        config = combinaciones[combinacion_id]

        desde = _ultima_repeticion_presente(args.salida_dir, combinacion_id) + 1
        if desde > args.repeticiones:
            logger.info(
                "combinacion=%s ya tiene %d repeticiones (>= %d pedidas); no se hace nada",
                combinacion_id, desde - 1, args.repeticiones,
            )
            continue

        for repeticion in range(desde, args.repeticiones + 1):
            salida = os.path.join(args.salida_dir, f"comb{combinacion_id}_rep{repeticion}.jsonl")
            logger.info(
                "iniciando combinacion=%s repeticion=%d/%d salida=%s",
                combinacion_id, repeticion, args.repeticiones, salida,
            )
            _limpiar_estado_redis(args.redis_host, args.redis_port)
            args.salida = salida
            ejecutar(combinacion_id, str(repeticion), config, args)
            logger.info(
                "terminada combinacion=%s repeticion=%d/%d",
                combinacion_id, repeticion, args.repeticiones,
            )

    logger.info("lote_formal_completo")


if __name__ == "__main__":
    main()
