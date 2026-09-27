"""
analizar_calibracion.py
=========================
Calcula la tasa de falsos positivos de una o varias corridas de calibración
del umbral heurístico de `monitor-accesos-indebidos`, cuando esas corridas se
generaron invocando directamente `generador_trafico_legitimo.py` en vez de
`ejecutar_combinacion.py`.

Por qué existe este script aparte de `analizar_resultados.py`:
`analizar_resultados.py` solo sabe leer archivos producidos por
`ejecutar_combinacion.py` (que envuelven cada línea con un campo
"tipo_linea" y ya traen embebidos los eventos de `registro-auditoria`
correspondientes a esa corrida). `generador_trafico_legitimo.py`, en cambio,
escribe un .jsonl "crudo": una línea por solicitud, tal cual la devuelve
`consultar_perfil()` en `comun.py`, sin ese envoltorio. Este script lee ese
formato crudo y, para cada archivo, consulta en vivo `GET /eventos` de
`registro-auditoria` en el rango de tiempo que cubrió la corrida, en vez de
esperar que esos eventos ya vengan incluidos.

Como todo el tráfico de una corrida de calibración es legítimo por
construcción (no se inyecta ningún ataque), cualquier hallazgo cuyo
actor_id/client_id_consultado coincida con una solicitud del archivo es, por
definición, un falso positivo.

Uso:
    python analizar_calibracion.py \
        --corrida resultados/calib_div5.jsonl:5 \
        --corrida resultados/calib_div8.jsonl:8 \
        --corrida resultados/calib_div12.jsonl:12
"""

import argparse
import json
from datetime import datetime, timedelta

import requests


def _parsear_timestamp(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _cargar_solicitudes_crudas(ruta: str) -> list:
    """
    Lee un .jsonl escrito directamente por generador_trafico_legitimo.py:
    una línea por solicitud, sin envoltorio "tipo_linea".
    """
    solicitudes = []
    with open(ruta, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if linea:
                solicitudes.append(json.loads(linea))
    if not solicitudes:
        raise ValueError(f"El archivo '{ruta}' no tiene ninguna solicitud registrada")
    return solicitudes


def _consultar_hallazgos(url_registro_auditoria, desde, hasta):
    respuesta = requests.get(
        f"{url_registro_auditoria}/eventos",
        params={"tipo_registro": "hallazgo", "desde": desde, "hasta": hasta},
        timeout=15,
    )
    respuesta.raise_for_status()
    return respuesta.json()


def analizar_corrida(ruta: str, umbral_diversidad, url_registro_auditoria: str, margen_segundos: float):
    solicitudes = _cargar_solicitudes_crudas(ruta)

    envios = [_parsear_timestamp(s["timestamp_envio"]) for s in solicitudes]
    desde = min(envios)
    # Margen adicional tras la última solicitud: da tiempo a que la
    # detección y la escritura en registro-auditoria terminen de propagarse
    # antes de cerrar la ventana de búsqueda.
    hasta = max(envios) + timedelta(seconds=margen_segundos)

    hallazgos = _consultar_hallazgos(
        url_registro_auditoria,
        desde.isoformat().replace("+00:00", "Z"),
        hasta.isoformat().replace("+00:00", "Z"),
    )

    pares_solicitud = {(s["actor_id"], s["client_id_consultado"]) for s in solicitudes}
    falsos_positivos = [
        h for h in hallazgos
        if (h.get("actor_id"), h.get("client_id_consultado")) in pares_solicitud
    ]

    total = len(solicitudes)
    num_fp = len(falsos_positivos)
    tasa_fp = num_fp / total if total else None

    return {
        "archivo": ruta,
        "umbral_diversidad": umbral_diversidad,
        "num_solicitudes_legitimas": total,
        "num_falsos_positivos": num_fp,
        "tasa_falsos_positivos": tasa_fp,
        "hallazgos_falsos_positivos": falsos_positivos,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Calcula la tasa de falsos positivos de corridas de calibración generadas "
                    "directamente con generador_trafico_legitimo.py."
    )
    parser.add_argument(
        "--corrida", action="append", required=True, dest="corridas",
        metavar="ARCHIVO:UMBRAL",
        help="Un archivo .jsonl de calibración y el UMBRAL_DIVERSIDAD que estaba activo cuando se "
             "generó, separados por ':'. Se puede repetir para comparar varias corridas en una sola "
             "tabla, por ejemplo: --corrida resultados/calib_div5.jsonl:5",
    )
    parser.add_argument("--url-registro-auditoria", default="http://localhost:8004")
    parser.add_argument(
        "--margen-segundos", type=float, default=5,
        help="Margen tras la última solicitud de la corrida que se incluye en la búsqueda de "
             "hallazgos, para no perder detecciones tardías.",
    )
    args = parser.parse_args()

    resultados = []
    for entrada in args.corridas:
        if ":" not in entrada:
            raise SystemExit(f"'--corrida {entrada}' debe tener la forma ARCHIVO:UMBRAL")
        ruta, umbral = entrada.rsplit(":", 1)
        resultados.append(
            analizar_corrida(ruta, umbral, args.url_registro_auditoria, args.margen_segundos)
        )

    print(f"{'umbral':>8}  {'solicitudes':>12}  {'falsos_positivos':>17}  {'tasa_fp':>9}   archivo")
    for r in resultados:
        tasa = f"{r['tasa_falsos_positivos']:.2%}" if r["tasa_falsos_positivos"] is not None else "n/a"
        print(
            f"{r['umbral_diversidad']:>8}  {r['num_solicitudes_legitimas']:>12}  "
            f"{r['num_falsos_positivos']:>17}  {tasa:>9}   {r['archivo']}"
        )

    for r in resultados:
        if r["hallazgos_falsos_positivos"]:
            print(f"\nDetalle de falsos positivos — umbral {r['umbral_diversidad']} ({r['archivo']}):")
            for h in r["hallazgos_falsos_positivos"]:
                print(
                    f"  actor_id={h.get('actor_id')} client_id_consultado={h.get('client_id_consultado')} "
                    f"razon={h.get('razon')} timestamp_deteccion={h.get('timestamp_deteccion')}"
                )


if __name__ == "__main__":
    main()
