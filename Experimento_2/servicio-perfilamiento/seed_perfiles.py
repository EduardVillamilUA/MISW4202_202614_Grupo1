"""
seed_perfiles.py
================
Script independiente de siembra de perfiles simulados en Redis.

No forma parte del proceso Flask ni se ejecuta en cada arranque del
contenedor: se corre una única vez (o cada vez que se quiera reiniciar el
contenido de los perfiles) antes de lanzar una corrida del experimento.

Es idempotente por construcción: todos los valores que escribe se derivan de
forma determinística del índice del perfil, así que ejecutarlo varias veces
siempre produce exactamente los mismos datos (HSET simplemente los
sobrescribe con los mismos valores, nunca los duplica ni los corrompe).

Uso:
    python seed_perfiles.py [--cantidad N]

Variables de entorno:
    REDIS_HOST (por defecto "redis")
    REDIS_PORT (por defecto 6379)
"""

import argparse
import os
import sys

import redis


REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

CANTIDAD_MINIMA = 50

# Fecha fija (no la hora real de ejecución) para que "actualizado_en" también
# sea determinística: si se usara datetime.now(), dos ejecuciones del script
# escribirían valores distintos y el script dejaría de ser verificablemente
# idempotente.
ACTUALIZADO_EN_SIMULADO = "2026-01-01T00:00:00.000Z"


def generar_perfil(indice: int) -> dict:
    client_id = f"cli-{indice:03d}"
    # Fórmula arbitraria y determinística: solo se necesita que el score
    # varíe entre perfiles y quede siempre en el rango [0, 1); su valor no
    # tiene ningún significado de negocio en este experimento.
    score_riesgo = round(((indice * 37) % 100) / 100, 2)
    return {
        "client_id": client_id,
        "nombre_referencia": f"Cliente Simulado {indice:03d}",
        "score_riesgo": str(score_riesgo),
        "actualizado_en": ACTUALIZADO_EN_SIMULADO,
    }


def main():
    parser = argparse.ArgumentParser(description="Siembra perfiles simulados en Redis.")
    parser.add_argument(
        "--cantidad",
        type=int,
        default=CANTIDAD_MINIMA,
        help=f"Cantidad de perfiles a generar (mínimo {CANTIDAD_MINIMA}).",
    )
    args = parser.parse_args()

    cantidad = max(args.cantidad, CANTIDAD_MINIMA)

    cliente = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )

    try:
        cliente.ping()
    except redis.exceptions.RedisError as exc:
        print(f"No se pudo conectar a Redis en {REDIS_HOST}:{REDIS_PORT}: {exc}", file=sys.stderr)
        sys.exit(1)

    for indice in range(1, cantidad + 1):
        perfil = generar_perfil(indice)
        cliente.hset(f"perfil:{perfil['client_id']}", mapping=perfil)

    print(f"Perfiles sembrados: {cantidad} (cli-001 .. cli-{cantidad:03d})")


if __name__ == "__main__":
    main()
