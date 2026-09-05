"""
analizar_resultados.py — calcula las métricas de las 60 corridas y produce el veredicto.

Entradas (sección 5.1 de 04-harness-experimento-y-analisis.md):
    harness-corrida-{tipo}-{numero}.jsonl   (60 archivos, uno por corrida)
    ms-monitor.jsonl                        (acumulativo)
    ms-router.jsonl                         (acumulativo, no se usa para las 4 métricas oficiales
                                              pero se deja disponible para depuración manual)

Salidas (sección 5.3):
    resultados_consolidados.csv
    resumen_por_tipo_falla.csv
    boxplot_tiempo_deteccion.png
    boxplot_ventana_exposicion.png
    boxplot_tiempo_reintegracion.png
    veredicto impreso en consola

Umbrales (sección 4 de 06-protocolo-experimental-y-metricas.md):
    detección       <= 15 s
    exposición      <  2 %
    reintegración   <= 20 s
"""
import argparse
import glob
import os
from datetime import datetime

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

INSTANCIA_OBJETIVO = "instancia-3"
UMBRAL_DETECCION_S = 15
UMBRAL_EXPOSICION_PCT = 2.0
UMBRAL_REINTEGRACION_S = 20


def parse_args():
    p = argparse.ArgumentParser(description="Analiza los resultados de las 60 corridas")
    p.add_argument("--directorio-resultados", default="/resultados")
    return p.parse_args()


def cargar_jsonl(ruta: str) -> pd.DataFrame:
    if not os.path.exists(ruta) or os.path.getsize(ruta) == 0:
        return pd.DataFrame()
    return pd.read_json(ruta, lines=True)


def a_timestamp(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    return pd.to_datetime(valor, utc=True)


def analizar_corrida(archivo_harness: str, df_monitor: pd.DataFrame) -> dict:
    df_h = cargar_jsonl(archivo_harness)
    if df_h.empty:
        return None

    nombre = os.path.basename(archivo_harness)
    # nombre esperado: harness-corrida-{tipo}-{numero}.jsonl
    partes = nombre.replace(".jsonl", "").split("-")
    # ["harness","corrida", tipo..., numero]  (tipo puede tener guiones, ej. error_http no, pero por si acaso)
    numero_corrida = int(partes[-1])
    tipo_falla = "-".join(partes[2:-1])

    fila_inicio = df_h[df_h["evento"] == "inicio_corrida"]
    fila_fin = df_h[df_h["evento"] == "fin_corrida"]
    if fila_inicio.empty or fila_fin.empty:
        return {
            "tipo_falla": tipo_falla, "numero_corrida": numero_corrida,
            "tiempo_deteccion_s": None, "ventana_exposicion_pct": None,
            "tiempo_reintegracion_s": None, "falsos_positivos_pct": None,
            "cumple_deteccion": False, "cumple_exposicion": False, "cumple_reintegracion": False,
            "nota": "corrida incompleta: falta inicio_corrida o fin_corrida",
        }

    ts_inicio_corrida = a_timestamp(fila_inicio.iloc[0]["timestamp"])
    ts_fin_corrida = a_timestamp(fila_fin.iloc[0]["timestamp"])

    eventos_falla_inyectada = df_h[df_h["evento"] == "falla_inyectada"]
    eventos_falla_limpiada = df_h[df_h["evento"] == "falla_limpiada"]

    ts_falla_inyectada = a_timestamp(eventos_falla_inyectada.iloc[0]["timestamp"]) if not eventos_falla_inyectada.empty else None
    ts_falla_limpiada = a_timestamp(eventos_falla_limpiada.iloc[0]["timestamp"]) if not eventos_falla_limpiada.empty else None

    # --- Filtrar ms-monitor.jsonl a la ventana de tiempo de esta corrida ---
    if not df_monitor.empty:
        df_monitor["ts"] = df_monitor["timestamp"].apply(a_timestamp)
        ventana_monitor = df_monitor[(df_monitor["ts"] >= ts_inicio_corrida) & (df_monitor["ts"] <= ts_fin_corrida)]
    else:
        ventana_monitor = pd.DataFrame()

    # --- Métrica 1: Tiempo de detección ---
    tiempo_deteccion_s = None
    ts_deteccion = None
    if not ventana_monitor.empty and ts_falla_inyectada is not None:
        cambios_a_unhealthy = ventana_monitor[
            (ventana_monitor["evento"] == "cambio_estado")
            & (ventana_monitor.get("instancia_id") == INSTANCIA_OBJETIVO)
            & (ventana_monitor.get("estado_nuevo") == "unhealthy")
            & (ventana_monitor["ts"] >= ts_falla_inyectada)
        ].sort_values("ts")
        if not cambios_a_unhealthy.empty:
            ts_deteccion = cambios_a_unhealthy.iloc[0]["ts"]
            tiempo_deteccion_s = (ts_deteccion - ts_falla_inyectada).total_seconds()

    # --- Métrica 2: Ventana de exposición al cliente ---
    ventana_exposicion_pct = None
    if ts_falla_inyectada is not None and "solicitud_resultado" in df_h["evento"].values:
        df_res = df_h[df_h["evento"] == "solicitud_resultado"].copy()
        df_res["ts_resp"] = df_res["timestamp_respuesta"].apply(a_timestamp)
        limite_superior = ts_deteccion if ts_deteccion is not None else ts_fin_corrida
        en_ventana = df_res[(df_res["ts_resp"] >= ts_falla_inyectada) & (df_res["ts_resp"] <= limite_superior)]
        if len(en_ventana) > 0:
            incorrectas = (en_ventana["es_correcta"] == False).sum()  # noqa: E712
            ventana_exposicion_pct = 100.0 * incorrectas / len(en_ventana)

    # --- Métrica 3: Tiempo de reintegración ---
    tiempo_reintegracion_s = None
    if not ventana_monitor.empty and ts_falla_limpiada is not None:
        cambios_a_healthy = ventana_monitor[
            (ventana_monitor["evento"] == "cambio_estado")
            & (ventana_monitor.get("instancia_id") == INSTANCIA_OBJETIVO)
            & (ventana_monitor.get("estado_nuevo") == "healthy")
            & (ventana_monitor["ts"] >= ts_falla_limpiada)
        ].sort_values("ts")
        if not cambios_a_healthy.empty:
            ts_reintegracion = cambios_a_healthy.iloc[0]["ts"]
            tiempo_reintegracion_s = (ts_reintegracion - ts_falla_limpiada).total_seconds()

    # --- Métrica 4: Tasa de falsos positivos (instancias distintas a la objetivo) ---
    falsos_positivos_pct = None
    if not ventana_monitor.empty:
        probes_otras = ventana_monitor[
            (ventana_monitor["evento"] == "resultado_probe") & (ventana_monitor.get("instancia_id") != INSTANCIA_OBJETIVO)
        ]
        if len(probes_otras) > 0:
            fallos = (probes_otras["resultado"] == "fallo").sum()
            falsos_positivos_pct = 100.0 * fallos / len(probes_otras)

    return {
        "tipo_falla": tipo_falla,
        "numero_corrida": numero_corrida,
        "tiempo_deteccion_s": tiempo_deteccion_s,
        "ventana_exposicion_pct": ventana_exposicion_pct,
        "tiempo_reintegracion_s": tiempo_reintegracion_s,
        "falsos_positivos_pct": falsos_positivos_pct,
        "cumple_deteccion": (tiempo_deteccion_s is not None and tiempo_deteccion_s <= UMBRAL_DETECCION_S),
        "cumple_exposicion": (ventana_exposicion_pct is not None and ventana_exposicion_pct < UMBRAL_EXPOSICION_PCT),
        "cumple_reintegracion": (tiempo_reintegracion_s is not None and tiempo_reintegracion_s <= UMBRAL_REINTEGRACION_S),
        "nota": "",
    }


def graficar_boxplot(df: pd.DataFrame, columna: str, umbral, titulo: str, ylabel: str, archivo_salida: str):
    tipos = sorted(df["tipo_falla"].unique())
    datos = [df[df["tipo_falla"] == t][columna].dropna().tolist() for t in tipos]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.boxplot(datos, labels=tipos)
    ax.axhline(y=umbral, color="red", linestyle="--", label=f"Umbral = {umbral}")
    ax.set_title(titulo)
    ax.set_ylabel(ylabel)
    ax.legend()
    fig.tight_layout()
    fig.savefig(archivo_salida)
    plt.close(fig)
    print(f"Gráfica guardada: {archivo_salida}")


def imprimir_veredicto(df: pd.DataFrame):
    print("\n" + "=" * 70)
    print("VEREDICTO POR TIPO DE FALLA")
    print("=" * 70)
    for tipo in sorted(df["tipo_falla"].unique()):
        sub = df[df["tipo_falla"] == tipo]
        n = len(sub)
        pct_deteccion = 100.0 * sub["cumple_deteccion"].sum() / n
        pct_exposicion = 100.0 * sub["cumple_exposicion"].sum() / n
        pct_reintegracion = 100.0 * sub["cumple_reintegracion"].sum() / n

        respaldada = pct_deteccion >= 95 and pct_exposicion >= 95 and pct_reintegracion >= 95
        veredicto = "HIPÓTESIS RESPALDADA" if respaldada else "HIPÓTESIS RECHAZADA (revisar causa raíz, sección 5 de 06)"

        print(f"\n--- {tipo} ({n} corridas) ---")
        print(f"  Detección    ≤ {UMBRAL_DETECCION_S}s : {pct_deteccion:.0f}% de las corridas cumplen")
        print(f"  Exposición   < {UMBRAL_EXPOSICION_PCT}%  : {pct_exposicion:.0f}% de las corridas cumplen")
        print(f"  Reintegración ≤ {UMBRAL_REINTEGRACION_S}s : {pct_reintegracion:.0f}% de las corridas cumplen")
        print(f"  => {veredicto}")

    print("\nNOTA: este veredicto automático es un apoyo de lectura rápida (criterio: ≥95% de las 20")
    print("corridas cumpliendo cada umbral). La decisión FINAL sobre si la hipótesis se respalda o se")
    print("rechaza es del equipo, revisando los datos reales, según GUIA-EQUIPO-EJECUCION.md, Paso 6.")


def main():
    args = parse_args()
    d = args.directorio_resultados

    archivos_harness = sorted(glob.glob(os.path.join(d, "harness-corrida-*.jsonl")))
    if not archivos_harness:
        print(f"[ERROR] No se encontraron archivos harness-corrida-*.jsonl en {d}")
        return

    df_monitor = cargar_jsonl(os.path.join(d, "ms-monitor.jsonl"))

    filas = []
    for archivo in archivos_harness:
        fila = analizar_corrida(archivo, df_monitor)
        if fila is not None:
            filas.append(fila)

    df = pd.DataFrame(filas)
    ruta_csv = os.path.join(d, "resultados_consolidados.csv")
    df.to_csv(ruta_csv, index=False)
    print(f"CSV consolidado guardado: {ruta_csv} ({len(df)} filas)")

    # --- Resumen estadístico por tipo de falla ---
    resumen_filas = []
    for tipo in sorted(df["tipo_falla"].unique()):
        sub = df[df["tipo_falla"] == tipo]
        n = len(sub)
        resumen_filas.append({
            "tipo_falla": tipo,
            "n_corridas": n,
            "deteccion_media_s": sub["tiempo_deteccion_s"].mean(),
            "deteccion_mediana_s": sub["tiempo_deteccion_s"].median(),
            "deteccion_p95_s": sub["tiempo_deteccion_s"].quantile(0.95),
            "deteccion_min_s": sub["tiempo_deteccion_s"].min(),
            "deteccion_max_s": sub["tiempo_deteccion_s"].max(),
            "pct_cumple_deteccion": 100.0 * sub["cumple_deteccion"].sum() / n,
            "exposicion_media_pct": sub["ventana_exposicion_pct"].mean(),
            "exposicion_p95_pct": sub["ventana_exposicion_pct"].quantile(0.95),
            "pct_cumple_exposicion": 100.0 * sub["cumple_exposicion"].sum() / n,
            "reintegracion_media_s": sub["tiempo_reintegracion_s"].mean(),
            "reintegracion_p95_s": sub["tiempo_reintegracion_s"].quantile(0.95),
            "pct_cumple_reintegracion": 100.0 * sub["cumple_reintegracion"].sum() / n,
        })
    df_resumen = pd.DataFrame(resumen_filas)
    ruta_resumen = os.path.join(d, "resumen_por_tipo_falla.csv")
    df_resumen.to_csv(ruta_resumen, index=False)
    print(f"\nResumen por tipo de falla guardado: {ruta_resumen}")
    print(df_resumen.to_string(index=False))

    # --- Gráficas ---
    graficar_boxplot(df, "tiempo_deteccion_s", UMBRAL_DETECCION_S,
                      "Tiempo de detección por tipo de falla", "segundos",
                      os.path.join(d, "boxplot_tiempo_deteccion.png"))
    graficar_boxplot(df, "ventana_exposicion_pct", UMBRAL_EXPOSICION_PCT,
                      "Ventana de exposición al cliente por tipo de falla", "% de solicitudes incorrectas",
                      os.path.join(d, "boxplot_ventana_exposicion.png"))
    graficar_boxplot(df, "tiempo_reintegracion_s", UMBRAL_REINTEGRACION_S,
                      "Tiempo de reintegración por tipo de falla", "segundos",
                      os.path.join(d, "boxplot_tiempo_reintegracion.png"))

    imprimir_veredicto(df)


if __name__ == "__main__":
    main()
