"""
seed_actores.py
================
Genera de forma programática el archivo del directorio estático de actores
(el mismo formato que lee api-gateway al arrancar), con una cantidad
configurable de actores por rol, en vez de escribirlo a mano.

Los client_id_propio que se asignan a los actores 'cliente' siguen el mismo
formato "cli-{índice:03d}" que usa seed_perfiles.py (del servicio de
perfilamiento) para nombrar los perfiles simulados, así que conviene sembrar
al menos tantos perfiles como actores 'cliente' se generen aquí para que
ninguno quede apuntando a un perfil inexistente.

Es idempotente: siempre genera el mismo contenido para los mismos parámetros
(los índices se asignan en orden, no al azar), así que volver a ejecutarlo
simplemente sobrescribe el archivo de salida con el mismo resultado.

Uso:
    python seed_actores.py --num-clientes 10 --num-asesores 4 --num-operaciones 2
"""

import argparse
import json
import os

RUTA_SALIDA_POR_DEFECTO = "../api-gateway/config/actores.json"


def generar_directorio(num_clientes: int, num_asesores: int, num_operaciones: int) -> dict:
    directorio = {}
    for i in range(1, num_clientes + 1):
        directorio[f"actor-cli-{i:03d}"] = {"rol": "cliente", "client_id_propio": f"cli-{i:03d}"}
    for i in range(1, num_asesores + 1):
        directorio[f"actor-ase-{i:03d}"] = {"rol": "asesor", "client_id_propio": None}
    for i in range(1, num_operaciones + 1):
        directorio[f"actor-ops-{i:03d}"] = {"rol": "operaciones", "client_id_propio": None}
    return directorio


def main():
    parser = argparse.ArgumentParser(description="Genera el directorio estático de actores del experimento.")
    parser.add_argument("--num-clientes", type=int, default=10)
    parser.add_argument("--num-asesores", type=int, default=4)
    parser.add_argument("--num-operaciones", type=int, default=2)
    parser.add_argument("--salida", default=RUTA_SALIDA_POR_DEFECTO)
    args = parser.parse_args()

    directorio = generar_directorio(args.num_clientes, args.num_asesores, args.num_operaciones)

    ruta_absoluta = os.path.abspath(args.salida)
    os.makedirs(os.path.dirname(ruta_absoluta), exist_ok=True)
    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(directorio, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        f"Directorio de actores generado en {args.salida}: "
        f"{args.num_clientes} clientes, {args.num_asesores} asesores, "
        f"{args.num_operaciones} operaciones."
    )
    print(
        f"Recuerda sembrar al menos {args.num_clientes} perfiles simulados "
        "(seed_perfiles.py --cantidad) para que todos los client_id_propio existan."
    )


if __name__ == "__main__":
    main()
