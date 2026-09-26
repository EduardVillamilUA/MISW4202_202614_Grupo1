"""
analizar_resultados.py
========================
Calcula las métricas del experimento a partir de uno o varios archivos
producidos por ejecutar_combinacion.py, y genera una tabla por corrida, una
tabla agregada por combinación, y un par de gráficas.

No vuelve a consultar ningún servicio en vivo: todo lo que necesita
(solicitudes enviadas por el harness y eventos recuperados de
registro-auditoria) ya está en los archivos de entrada.

Uso:
    python analizar_resultados.py resultados/comb2_rep1.jsonl resultados/comb2_rep2.jsonl ...
"""

import argparse
import glob
import json
import logging
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s analizar-resultados %(levelname)s %(message)s")
logger = logging.getLogger("analizar-resultados")


def _parsear_timestamp(ts: str) -> datetime:
    # A partir de Python 3.11, fromisoformat() acepta directamente el sufijo
    # 'Z' (UTC) que usan todos los timestamps de este experimento.
    return datetime.fromisoformat(ts)


def _percentil_95(valores):
    if not valores:
        return None
    return float(pd.Series(valores, dtype=float).quantile(0.95))


def cargar_corrida(ruta: str) -> dict:
    metadata = None
    solicitudes = []
    eventos_auditoria = []
    with open(ruta, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            registro = json.loads(linea)
            tipo = registro.pop("tipo_linea", None)
            if tipo == "metadata":
                metadata = registro
            elif tipo == "solicitud":
                solicitudes.append(registro)
            elif tipo == "evento_auditoria":
                eventos_auditoria.append(registro)
    if metadata is None:
        raise ValueError(f"El archivo '{ruta}' no tiene una línea de metadata; ¿fue generado por ejecutar_combinacion.py?")
    return {"ruta": ruta, "metadata": metadata, "solicitudes": solicitudes, "eventos_auditoria": eventos_auditoria}


def _hallazgo_coincide(solicitud, evento, ventana_correlacion_segundos):
    if evento.get("tipo_registro") != "hallazgo":
        return False
    if evento.get("actor_id") != solicitud["actor_id"]:
        return False
    if evento.get("client_id_consultado") != solicitud["client_id_consultado"]:
        return False
    envio = _parsear_timestamp(solicitud["timestamp_envio"])
    deteccion = _parsear_timestamp(evento["timestamp_deteccion"])
    delta_segundos = (deteccion - envio).total_seconds()
    # No se acepta una detección anterior al envío (sería una coincidencia
    # casual con un hallazgo de otra solicitud), y se acota por arriba con
    # la ventana de correlación para no emparejar con hallazgos demasiado
    # tardíos como para ser razonablemente atribuibles a esta solicitud.
    return 0 <= delta_segundos <= ventana_correlacion_segundos


def calcular_metricas_corrida(corrida: dict, ventana_correlacion_segundos: float) -> dict:
    solicitudes = corrida["solicitudes"]
    eventos = corrida["eventos_auditoria"]
    hallazgos = [e for e in eventos if e.get("tipo_registro") == "hallazgo"]
    reacciones = [e for e in eventos if e.get("tipo_registro") == "reaccion"]

    ataques = [s for s in solicitudes if s.get("es_ataque_inyectado")]
    legitimas = [s for s in solicitudes if s.get("es_trafico_legitimo")]

    latencias_deteccion_ms = []
    detectados = 0
    for solicitud in ataques:
        coincidencias = [e for e in hallazgos if _hallazgo_coincide(solicitud, e, ventana_correlacion_segundos)]
        if coincidencias:
            detectados += 1
            primero = min(coincidencias, key=lambda e: e["timestamp_deteccion"])
            envio = _parsear_timestamp(solicitud["timestamp_envio"])
            deteccion = _parsear_timestamp(primero["timestamp_deteccion"])
            latencias_deteccion_ms.append((deteccion - envio).total_seconds() * 1000)

    falsos_positivos = sum(
        1 for solicitud in legitimas
        if any(_hallazgo_coincide(solicitud, e, ventana_correlacion_segundos) for e in hallazgos)
    )

    latencias_reaccion_ms = []
    for hallazgo in hallazgos:
        # La reacción "real" (no duplicada) asociada a este hallazgo es la
        # que determina la latencia de reacción; una eventual reacción
        # marcada como duplicada no representa un bloqueo nuevo y no debe
        # contarse como una segunda medición de la misma latencia.
        reaccion_real = next(
            (r for r in reacciones if r.get("evento_id") == hallazgo.get("evento_id") and not r.get("es_duplicado")),
            None,
        )
        if reaccion_real and reaccion_real.get("timestamp_reaccion"):
            deteccion = _parsear_timestamp(hallazgo["timestamp_deteccion"])
            reaccion_ts = _parsear_timestamp(reaccion_real["timestamp_reaccion"])
            latencias_reaccion_ms.append((reaccion_ts - deteccion).total_seconds() * 1000)

    # Idempotencia: se agrupan las reacciones por evento_id. Un grupo con
    # más de una reacción indica que ese hallazgo llegó duplicado (real o
    # simulado). Para confirmar, mirando el propio registro, que la acción
    # de bloqueo solo se ejecutó una vez, basta verificar que a lo sumo una
    # reacción del grupo esté marcada como no-duplicada: es justo la
    # invariante que garantiza el chequeo de idempotencia de
    # orquestador-reaccion (SADD solo se ejecuta en la rama "primera vez").
    reacciones_por_evento = {}
    for r in reacciones:
        reacciones_por_evento.setdefault(r.get("evento_id"), []).append(r)
    grupos_duplicados = {eid: rs for eid, rs in reacciones_por_evento.items() if len(rs) > 1}
    grupos_correctos = sum(
        1 for rs in grupos_duplicados.values()
        if sum(0 if r.get("es_duplicado") else 1 for r in rs) <= 1
    )

    return {
        "num_ataques_inyectados": len(ataques),
        "num_ataques_detectados": detectados,
        "tasa_deteccion": (detectados / len(ataques)) if ataques else None,
        "num_legitimas": len(legitimas),
        "num_falsos_positivos": falsos_positivos,
        "tasa_falsos_positivos": (falsos_positivos / len(legitimas)) if legitimas else None,
        "latencia_deteccion_p95_ms": _percentil_95(latencias_deteccion_ms),
        "latencia_reaccion_p95_ms": _percentil_95(latencias_reaccion_ms),
        "num_grupos_hallazgos_duplicados": len(grupos_duplicados),
        "tasa_reacciones_idempotentes_correctas": (
            (grupos_correctos / len(grupos_duplicados)) if grupos_duplicados else None
        ),
        "latencias_deteccion_ms": latencias_deteccion_ms,
    }


def construir_tabla(corridas, metricas_por_corrida) -> pd.DataFrame:
    filas = []
    for corrida, metricas in zip(corridas, metricas_por_corrida):
        fila = dict(corrida["metadata"])
        fila.update({k: v for k, v in metricas.items() if k != "latencias_deteccion_ms"})
        filas.append(fila)
    return pd.DataFrame(filas)


def agregar_por_combinacion(tabla: pd.DataFrame) -> pd.DataFrame:
    columnas_metricas = [
        "tasa_deteccion", "tasa_falsos_positivos", "latencia_deteccion_p95_ms",
        "latencia_reaccion_p95_ms", "tasa_reacciones_idempotentes_correctas",
    ]
    filas = []
    for combinacion, grupo in tabla.groupby("combinacion"):
        fila = {"combinacion": combinacion, "num_repeticiones": len(grupo)}
        for columna in columnas_metricas:
            valores = grupo[columna].dropna()
            fila[f"{columna}_promedio"] = valores.mean() if not valores.empty else None
            fila[f"{columna}_p95"] = valores.quantile(0.95) if not valores.empty else None
        filas.append(fila)
    return pd.DataFrame(filas)


def graficar_fp_vs_umbral(tabla: pd.DataFrame, ruta_salida: str):
    datos = tabla.dropna(subset=["umbral_bajo_prueba", "tasa_falsos_positivos"])
    if datos.empty:
        logger.warning(
            "sin_datos_para_grafica_fp_vs_umbral: ninguna corrida trae --umbral-bajo-prueba; "
            "pásalo al ejecutar ejecutar_combinacion.py durante la calibración para poder generar esta gráfica"
        )
        return
    resumen = datos.groupby("umbral_bajo_prueba")["tasa_falsos_positivos"].mean().sort_index()
    fig, ax = plt.subplots()
    ax.plot(resumen.index, resumen.values, marker="o")
    ax.set_xlabel("Umbral bajo prueba")
    ax.set_ylabel("Tasa de falsos positivos")
    ax.set_title("Tasa de falsos positivos vs. umbral de calibración")
    fig.savefig(ruta_salida)
    plt.close(fig)
    logger.info("grafica_guardada ruta=%s", ruta_salida)


def graficar_distribucion_latencias(corridas, metricas_por_corrida, ruta_salida: str):
    datos_por_combinacion = {}
    for corrida, metricas in zip(corridas, metricas_por_corrida):
        combinacion = str(corrida["metadata"].get("combinacion"))
        if combinacion not in ("2", "3"):
            continue
        datos_por_combinacion.setdefault(combinacion, []).extend(metricas["latencias_deteccion_ms"])

    if not datos_por_combinacion:
        logger.warning("sin_datos_para_grafica_distribucion_latencias: no hay corridas de combinación 2 o 3 con detecciones")
        return

    etiquetas = sorted(datos_por_combinacion.keys())
    fig, ax = plt.subplots()
    ax.boxplot([datos_por_combinacion[e] for e in etiquetas], tick_labels=[f"Combinación {e}" for e in etiquetas])
    ax.set_ylabel("Latencia de detección (ms)")
    ax.set_title("Distribución de latencia de detección — combinaciones 2 y 3")
    fig.savefig(ruta_salida)
    plt.close(fig)
    logger.info("grafica_guardada ruta=%s", ruta_salida)


def main():
    parser = argparse.ArgumentParser(description="Calcula métricas y genera gráficas a partir de corridas del experimento.")
    parser.add_argument("entradas", nargs="+", help="Archivos .jsonl producidos por ejecutar_combinacion.py (admite comodines)")
    parser.add_argument("--ventana-correlacion-segundos", type=float, default=30)
    parser.add_argument("--salida-tabla", default="resultados/metricas_por_corrida.csv")
    parser.add_argument("--salida-tabla-agregada", default="resultados/metricas_por_combinacion.csv")
    parser.add_argument("--salida-graficas-dir", default="resultados/graficas")
    args = parser.parse_args()

    rutas = sorted({ruta for patron in args.entradas for ruta in glob.glob(patron)} or set(args.entradas))
    if not rutas:
        raise SystemExit("Ningún archivo de entrada coincide con los patrones dados")

    corridas = [cargar_corrida(ruta) for ruta in rutas]
    metricas_por_corrida = [calcular_metricas_corrida(c, args.ventana_correlacion_segundos) for c in corridas]

    tabla = construir_tabla(corridas, metricas_por_corrida)
    os.makedirs(os.path.dirname(os.path.abspath(args.salida_tabla)) or ".", exist_ok=True)
    tabla.to_csv(args.salida_tabla, index=False)
    logger.info("tabla_por_corrida_guardada ruta=%s filas=%d", args.salida_tabla, len(tabla))

    tabla_agregada = agregar_por_combinacion(tabla)
    os.makedirs(os.path.dirname(os.path.abspath(args.salida_tabla_agregada)) or ".", exist_ok=True)
    tabla_agregada.to_csv(args.salida_tabla_agregada, index=False)
    logger.info("tabla_agregada_guardada ruta=%s filas=%d", args.salida_tabla_agregada, len(tabla_agregada))

    os.makedirs(args.salida_graficas_dir, exist_ok=True)
    graficar_fp_vs_umbral(tabla, os.path.join(args.salida_graficas_dir, "fp_vs_umbral.png"))
    graficar_distribucion_latencias(
        corridas, metricas_por_corrida, os.path.join(args.salida_graficas_dir, "distribucion_latencias_deteccion.png")
    )

    print(tabla_agregada.to_string(index=False))


if __name__ == "__main__":
    main()
