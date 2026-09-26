# harness

Instrumento de medición del experimento de disponibilidad. No forma parte del
sistema enrutado: es la herramienta que **genera tráfico**, **inyecta y limpia
fallas** en momentos programados, y **calcula las métricas** a partir de los
registros de todos los componentes.

Contiene tres scripts de línea de comandos (Python 3.11, sin servidor web):

| Script | Rol |
| --- | --- |
| `harness.py` | Ejecuta **una** corrida: tráfico + inyección/limpieza de una falla + registro de cada solicitud |
| `ejecutar_experimento.py` | Orquesta las **60 corridas** (20 por cada tipo de falla) y, al terminar, lanza el análisis |
| `analizar_resultados.py` | Lee todos los registros, calcula métricas, genera tablas (CSV), gráficas (PNG) y un veredicto |

Todo el tráfico usa un **payload sintético fijo**, cuya respuesta correcta es
siempre `prima = 15000.0`:

```json
{"cliente_id": "SINTETICO-CANARY", "producto": "viaje", "monto_asegurado": 1000000}
```

---

## 1. `harness.py` — una corrida

### Parámetros

```
python harness.py \
  --tipo-falla {timeout|error_http|prima_inconsistente} \
  --numero-corrida <entero> \
  --router-url http://ms-router:8000 \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --archivo-log /resultados/harness-corrida-timeout-1.jsonl
```

Todos los parámetros son configurables por línea de comandos; nada está fijo en el
código, de modo que el mismo script sirve para las 60 corridas.

### Flujo de una corrida

| Momento | Acción |
| --- | --- |
| Calentamiento | 30 solicitudes al enrutador; se comprueba que ninguna da error |
| `t = 0` | Inicia el tráfico sostenido a **10 solicitudes/segundo** |
| `t = 20 s` | Inyecta la falla **directamente en la réplica objetivo** (`POST /inject-fault`), no a través del enrutador |
| `t = 60 s` | Limpia la falla (`POST /clear-fault`) |
| `t = 100 s` | Fin de la corrida; se cierra el archivo de registro |

Los instantes 20, 60 y 100 se miden como desfase desde `t = 0` (la primera solicitud
del tráfico sostenido), no con relojes independientes, para evitar desviaciones por
la duración variable del calentamiento. El tráfico se genera con un grupo de hilos,
de modo que una solicitud lenta (por ejemplo durante la falla `timeout`) no retrase
las siguientes.

### Validación de cada respuesta

Una respuesta se considera **correcta** solo si el código HTTP es `200` **y** la
prima recibida es `15000.0` (tolerancia `0.01`). Un código distinto de `200`, la
ausencia de respuesta (timeout de cliente de 5 s) o una prima distinta la marcan
como **incorrecta**.

### Eventos que registra (`--archivo-log`, JSON Lines)

| `evento` | Campos propios |
| --- | --- |
| `inicio_corrida` | `tipo_falla`, `numero_corrida`, `instancia_objetivo` |
| `fin_calentamiento` | `solicitudes_calentamiento`, `errores_calentamiento` |
| `solicitud_enviada` | `id_solicitud`, `timestamp_envio` |
| `solicitud_resultado` | `id_solicitud`, `timestamp_respuesta`, `codigo_http`, `prima_recibida`, `instancia_id_respuesta`, `es_correcta`, `latencia_ms`, `detalle` |
| `falla_inyectada` | `instancia_objetivo`, `tipo_falla`, `respuesta_inject_fault` |
| `falla_limpiada` | `instancia_objetivo`, `respuesta_clear_fault` |
| `fin_corrida` | `total_solicitudes`, `total_correctas`, `total_incorrectas` |

La inyección y la limpieza se reintentan un par de veces si la réplica no responde
al primer intento, y el resultado final queda registrado.

## 2. `ejecutar_experimento.py` — las 60 corridas

### Parámetros

```
python ejecutar_experimento.py \
  --router-url http://ms-router:8000 \
  --monitor-status-url http://ms-monitor:6000/status \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --instancias instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000 \
  --directorio-resultados /resultados
```

### Qué hace

Para cada tipo de falla (`timeout`, `error_http`, `prima_inconsistente`), 20 veces:

1. Limpia cualquier falla activa en las tres réplicas.
2. Espera a que el monitor confirme que las tres están `healthy` (sondea su
   `/status` cada 2 s, hasta ~30 s). Si no lo logra, **detiene la ejecución** para
   no acumular corridas inválidas.
3. Ejecuta `harness.py` con el tipo de falla y el número de corrida, escribiendo en
   `harness-corrida-{tipo}-{numero}.jsonl`.
4. Espera a que termine antes de iniciar la siguiente.

Al completar las 60 corridas, invoca `analizar_resultados.py`. Duración total
aproximada: **~2 horas**.

## 3. `analizar_resultados.py` — métricas y veredicto

### Parámetros

```
python analizar_resultados.py --directorio-resultados /resultados
```

### Entradas

- Los 60 archivos `harness-corrida-*.jsonl`.
- `ms-monitor.jsonl` (acumulativo de toda la ejecución).
- `ms-router.jsonl` (acumulativo; disponible para inspección manual).

Como los registros del monitor y del enrutador son acumulativos, cada corrida se
aísla filtrando sus eventos por el rango de tiempo entre `inicio_corrida` y
`fin_corrida` de esa corrida.

### Métricas por corrida y umbrales

| Métrica | Cómo se calcula | Umbral |
| --- | --- | --- |
| **Tiempo de detección** | `timestamp` del `cambio_estado` de la réplica objetivo a `unhealthy` − `timestamp` de `falla_inyectada` | ≤ 15 s |
| **Ventana de exposición** | Solicitudes incorrectas entre la inyección y la detección, divididas entre el **total** de solicitudes de tráfico sostenido de la corrida completa | < 2 % |
| **Tiempo de reintegración** | `timestamp` del `cambio_estado` de vuelta a `healthy` − `timestamp` de `falla_limpiada` | ≤ 20 s |
| **Tasa de falsos positivos** | % de chequeos con `resultado = "fallo"` en las réplicas que **no** recibieron falla | 0 % (o documentar) |

> El denominador de la ventana de exposición es el total de la corrida, **no** el
> número de solicitudes dentro de la ventana: así la métrica expresa qué parte de la
> experiencia completa del cliente se vio afectada.

### Salidas (en `--directorio-resultados`)

| Archivo | Contenido |
| --- | --- |
| `resultados_consolidados.csv` | Una fila por corrida (60) con las cuatro métricas y las banderas `cumple_deteccion` / `cumple_exposicion` / `cumple_reintegracion` |
| `resumen_por_tipo_falla.csv` | Media, mediana, p95, mínimo, máximo y % de corridas que cumplen cada umbral, agrupado por tipo de falla |
| `boxplot_tiempo_deteccion.png` | Diagrama de caja del tiempo de detección, con línea en 15 s |
| `boxplot_ventana_exposicion.png` | Diagrama de caja de la ventana de exposición, con línea en 2 % |
| `boxplot_tiempo_reintegracion.png` | Diagrama de caja del tiempo de reintegración, con línea en 20 s |
| Veredicto en consola | Por tipo de falla, % de corridas que cumple cada umbral y una etiqueta de resultado (criterio automático: ≥ 95 % de las 20 corridas). Es un apoyo de lectura rápida, no una conclusión definitiva |

## 4. Tecnología

- `requests` para las llamadas HTTP.
- `pandas` para leer y cruzar los archivos JSON Lines.
- `matplotlib` para las gráficas.
- `argparse` para los parámetros de línea de comandos.
- `threading` / `concurrent.futures` para generar tráfico a tasa constante sin
  bloquear el hilo principal.

## 5. Ejecución

### Con Docker

La imagen no define un comando por defecto; se le indica cuál script correr:

```bash
docker build -t harness .

# Una sola corrida
docker run --rm -v "$(pwd)/resultados:/resultados" harness \
  python harness.py --tipo-falla error_http --numero-corrida 1 \
  --router-url http://ms-router:8000 \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --archivo-log /resultados/harness-corrida-error_http-1.jsonl

# Solo el análisis, sobre registros ya existentes
docker run --rm -v "$(pwd)/resultados:/resultados" harness \
  python analizar_resultados.py --directorio-resultados /resultados
```

### En local (Python)

```bash
pip install -r requirements.txt
python analizar_resultados.py --directorio-resultados ./resultados
```

Necesita acceso por red al enrutador y a la réplica objetivo para `harness.py` y
`ejecutar_experimento.py`; `analizar_resultados.py` solo necesita los archivos de
registro.
