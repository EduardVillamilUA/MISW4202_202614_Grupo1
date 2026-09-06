# MISW4202_202614_Grupo1

Repositorio Proyecto Arquitecturas Ágiles de Software — Grupo 1.

**Experimento 1 de Disponibilidad** (HA-DISP-13 / HA-DISP-14) del caso Solventa.
Este README explica, de principio a fin, cómo se ejecuta el experimento y, sobre
todo, **cómo leer e interpretar los resultados** que quedan en la carpeta
`resultados/` para que cualquiera pueda sacar sus propias conclusiones.

---

## 1. Qué valida el experimento

El diseño síncrono de Cotización de Solventa (Cuaderno de Trabajo III) usa dos
tácticas de disponibilidad. El experimento las somete a fallas inyectadas de forma
controlada y mide si cumplen:

| Historia | Táctica | Qué exige | Métrica del experimento |
| --- | --- | --- | --- |
| **HA-DISP-13** | Detección (`ms-monitor`) | Detectar en **≤ 15 s** que una instancia de Cotización dejó de responder, responde con error técnico, o responde con una prima numéricamente incorrecta | Tiempo de detección |
| **HA-DISP-14** | Enmascaramiento (`ms-router`) | Excluir la instancia no saludable y reintegrarla al recuperarse, de forma **transparente** para el cliente (sin pérdida ni duplicación de solicitudes) | Ventana de exposición al cliente + Tiempo de reintegración |

Se prueban **tres tipos de falla**, con **20 repeticiones cada una** (60 corridas
en total):

| Tipo de falla | Qué simula | Qué chequeo del Monitor debería detectarla |
| --- | --- | --- |
| `timeout` | Caída / cuelgue del proceso (indisponibilidad total; `GET /health` no responde) | Chequeo de vida (Ping/Echo) |
| `error_http` | Falla técnica: el proceso vive pero `GET /health` responde `500` | Chequeo de vida (Ping/Echo) |
| `prima_inconsistente` | Falla de negocio: responde `200`, pero la prima está inflada un 50 % (`prima × 1.5`) | **Únicamente** el chequeo semántico — `GET /health` sigue respondiendo `200` con normalidad |

> `prima_inconsistente` es el caso más importante: es la razón de ser del chequeo
> semántico en el diseño. Si esta falla no se detecta dentro del umbral (o no se
> detecta), es evidencia directa de que el chequeo semántico no está cumpliendo su
> función.

El experimento **implementa** el diseño del Cuaderno III; **no lo rediseña**.

---

## 2. Estructura del repositorio

```
MISW4202_202614_Grupo1/
├── docker-compose.yml
├── .env                          # variables compartidas por ms-monitor y ms-router
├── .env.example                  # plantilla de .env
├── ms-cotizacion/                # Sebastián — servicio de negocio (x3 instancias)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── ms-monitor/                   # Fredy — Monitor de Salud (detección)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── ms-router/                    # Eduard — Router (enmascaramiento)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── harness/                      # Felipe — generador de tráfico, inyección de fallas y análisis
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── harness.py                 # ejecuta UNA corrida
│   ├── ejecutar_experimento.py    # orquesta las 60 corridas formales
│   └── analizar_resultados.py     # calcula métricas, gráficas y veredicto
└── resultados/                   # volumen compartido; se llena en tiempo de ejecución
                                   # (su contenido NO se versiona — ver .gitignore)
```

## 3. Componentes y puertos

| Servicio | Puerto interno | Puerto publicado al host | Rol |
| --- | --- | --- | --- |
| `ms-cotizacion-1/2/3` | 5000 | 5001 / 5002 / 5003 | Servicio bajo prueba. Misma imagen; cambia `INSTANCIA_ID`. La falla siempre se inyecta en **`instancia-3`** |
| `ms-monitor` | 6000 | 6000 | `GET /status` — solo depuración manual del equipo |
| `ms-router` | 8000 | 8000 | Fachada única. Punto de entrada del harness (`POST /cotizar`) |
| `redis` | 6379 | 6379 | Registro de Salud (claves `solventa:health:instancia-N` con valor `healthy` / `unhealthy`) |
| `harness` | — | — | Perfil `herramientas`; **no** arranca con `docker compose up` |

Dentro de la red de Docker los servicios se resuelven **por nombre** (p. ej.
`http://ms-router:8000`). Los puertos `5001/5002/5003`, `6000`, `8000` son solo para
pruebas manuales desde el host (`curl http://localhost:8000/...`).

---

## 4. Requisitos previos

- Docker + Docker Compose v2.
- Archivo `.env` en la raíz. Si no existe: `cp .env.example .env`.
- (Opcional) Python 3.11 local, solo si se prefiere correr el harness fuera de
  Docker contra los puertos publicados.

---

## 5. Proceso de ejecución

### Paso 0 — Crear la carpeta `resultados/`

**Es obligatorio crear la carpeta antes de levantar el stack.** Es el volumen
compartido donde los seis servicios escriben sus logs y donde el análisis deja sus
salidas. Su contenido está excluido de git (`.gitignore`: `resultados/*`).

```bash
mkdir -p resultados
```

Si se deja que Docker la cree sola, puede quedar con permisos de `root` y provocar
errores de escritura dentro de los contenedores; por eso se crea a mano.

### Paso 1 — Levantar el stack

```bash
docker compose up -d --build
docker compose ps      # los 6 servicios deben quedar "healthy" / "running"
```

Orden de arranque: `redis` → `ms-cotizacion-1/2/3` → `ms-monitor` (inicializa las
tres claves de Redis en `healthy`) → `ms-router`.

### Paso 2 — Prueba de integración manual (extremo a extremo)

Se ejecuta en vivo, **antes** de las 60 corridas formales.
Sirve para ver con los propios ojos que las piezas encajan.

```bash
# Monitor reporta las tres instancias "healthy"
curl http://localhost:6000/status

# Cotización de prueba por el Router: debe responder 200 con prima 15000.0
curl -X POST http://localhost:8000/cotizar \
  -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'

# Prueba de humo de DETECCIÓN: inyectar error_http en instancia-3
curl -X POST http://localhost:5003/inject-fault \
  -H "Content-Type: application/json" -d '{"tipo":"error_http"}'
sleep 18
curl http://localhost:6000/status          # instancia-3 debe aparecer "unhealthy"
# varias solicitudes al Router: el campo instancia_id nunca debe ser instancia-3

# Prueba de humo de REINTEGRACIÓN: limpiar la falla
curl -X POST http://localhost:5003/clear-fault
sleep 18
curl http://localhost:6000/status          # instancia-3 debe volver a "healthy"
```

### Paso 3 — Limpiar los logs de la prueba

Para no mezclar el tráfico de prueba con los datos oficiales:

```bash
rm -f resultados/*.jsonl
```

### Paso 4 — Ejecutar las 60 corridas formales

El servicio `harness` usa el perfil `herramientas`, así que se invoca de forma
explícita:

```bash
docker compose run --rm harness python ejecutar_experimento.py \
  --router-url http://ms-router:8000 \
  --monitor-status-url http://ms-monitor:6000/status \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --instancias instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000 \
  --directorio-resultados /resultados
```

Qué hace, corrida a corrida (20 × `timeout`, 20 × `error_http`, 20 × `prima_inconsistente`):

1. Reinicia el stack a estado sano (`clear-fault` en las tres instancias) y espera a
   que el Monitor confirme las tres `healthy` (si no lo logra en ~30 s, **aborta**
   para no acumular corridas inválidas).
2. Lanza `harness.py`, que ejecuta el flujo de tiempos de la corrida:
   - **Calentamiento**: 30 solicitudes; se confirma 0 % de error.
   - **t = 0**: inicia tráfico sostenido a **10 solicitudes/segundo**.
   - **t = 20 s**: inyecta la falla en `instancia-3` (`POST /inject-fault`, directo a
     la instancia, no por el Router).
   - **t = 60 s**: repara la falla (`POST /clear-fault`).
   - **t = 100 s**: fin de la corrida; cierra el log de esa corrida.
3. Al terminar las 60, invoca automáticamente `analizar_resultados.py`.

Duración total aproximada: **~2 horas**.

**Ejecutar una sola corrida (depuración):**

```bash
docker compose run --rm harness python harness.py \
  --tipo-falla error_http --numero-corrida 1 \
  --router-url http://ms-router:8000 \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --archivo-log /resultados/harness-corrida-error_http-1.jsonl
```

### Paso 5 — Apagar el stack

```bash
docker compose down
```

---

## 6. Qué hay en la carpeta `resultados/`

Todo el experimento deja evidencia en `resultados/`. Hay dos grupos de archivos.

### 6.1 Logs crudos — formato JSON Lines (un objeto JSON por línea)

Se escriben **durante** la ejecución. Todos comparten los campos `timestamp` (UTC,
ISO-8601 con microsegundos), `componente` y `evento`.

| Archivo | Lo escribe | Contenido | ¿Se reinicia? |
| --- | --- | --- | --- |
| `ms-cotizacion-instancia-1.jsonl`<br>`ms-cotizacion-instancia-2.jsonl`<br>`ms-cotizacion-instancia-3.jsonl` | cada instancia de Cotización | `inicio`, `health_check_recibido`, `cotizacion_recibida` (con `prima_calculada`, `resultado`), `falla_inyectada`, `falla_limpiada` | Acumulativo durante toda la ejecución del stack |
| `ms-monitor.jsonl` | `ms-monitor` | `inicio`, `ciclo_iniciado`, `resultado_probe` (uno por cada uno de los 6 chequeos por ciclo: 2 tipos × 3 instancias), **`cambio_estado`**, `escritura_redis` | **Acumulativo** — corre sin parar toda la ejecución |
| `ms-router.jsonl` | `ms-router` | `inicio`, `solicitud_recibida`, `estado_leido` (estado de las 3 claves de Redis leído en esa solicitud), `solicitud_enrutada` (con `instancia_elegida`, `resultado`, `codigo_http_respuesta`, `latencia_ms`), `solicitud_rechazada_sin_instancias` | **Acumulativo** — corre sin parar toda la ejecución |
| `harness-corrida-{tipo}-{n}.jsonl`<br>(60 archivos) | `harness` | `inicio_corrida`, `fin_calentamiento`, `solicitud_enviada`, **`solicitud_resultado`** (con `codigo_http`, `prima_recibida`, `instancia_id_respuesta`, **`es_correcta`**, `latencia_ms`, `detalle`), **`falla_inyectada`**, **`falla_limpiada`**, `fin_corrida` | Uno **nuevo por corrida** |

**Eventos que concentran la información:**

- `harness … falla_inyectada` / `falla_limpiada` → marcan el instante en que empieza
  y termina la falla en cada corrida.
- `ms-monitor … cambio_estado` (`instancia_id`, `estado_anterior`, `estado_nuevo`,
  `motivo`) → marca el instante en que el Monitor **detecta** (`estado_nuevo =
  unhealthy`) o **reintegra** (`estado_nuevo = healthy`). Es el evento más
  importante del experimento.
- `harness … solicitud_resultado` con `es_correcta` → dice, solicitud por solicitud,
  si el cliente recibió una respuesta correcta (HTTP 200 **y** `prima ≈ 15000.0`) o
  no (error HTTP, sin respuesta, o prima distinta).
- `ms-router … estado_leido` / `solicitud_enrutada` → permiten ver a qué instancia
  enrutó el Router en cada momento y por qué.

Como `ms-monitor.jsonl` y `ms-router.jsonl` son acumulativos, para analizar **una**
corrida se filtran sus eventos por el rango de tiempo entre el `timestamp` del
`inicio_corrida` y el del `fin_corrida` de esa corrida (esto es exactamente lo que
hace `analizar_resultados.py`).

### 6.2 Salidas del análisis

Se generan **al final**, cuando `analizar_resultados.py` termina:

| Archivo | Qué es |
| --- | --- |
| `resultados_consolidados.csv` | Una fila por corrida (60 filas) con las cuatro métricas y las banderas de cumplimiento |
| `resumen_por_tipo_falla.csv` | Estadísticos (media, mediana, p95, mín, máx) y % de corridas que cumplen cada umbral, agrupado por tipo de falla |
| `boxplot_tiempo_deteccion.png` | Diagrama de caja del tiempo de detección por tipo de falla, con línea roja en 15 s |
| `boxplot_ventana_exposicion.png` | Diagrama de caja de la ventana de exposición, con línea roja en 2 % |
| `boxplot_tiempo_reintegracion.png` | Diagrama de caja del tiempo de reintegración, con línea roja en 20 s |
| Veredicto en consola | Resumen por tipo de falla con "HIPÓTESIS RESPALDADA / RECHAZADA" |

---

## 7. Cómo leer e interpretar los resultados

### 7.1 Las cuatro métricas y sus umbrales

| Métrica | Qué mide, en palabras | Cómo se calcula (a partir de los logs) | Umbral |
| --- | --- | --- | --- |
| **Tiempo de detección** (`tiempo_deteccion_s`) | Cuánto tardó el Monitor en darse cuenta de la falla, desde que se inyectó hasta que escribió `unhealthy` para `instancia-3` | `timestamp` del `cambio_estado` a `unhealthy` (en `ms-monitor.jsonl`) − `timestamp` del `falla_inyectada` (en el harness de esa corrida) | **≤ 15 s** |
| **Ventana de exposición** (`ventana_exposicion_pct`) | Qué proporción de **toda** la experiencia del cliente durante la corrida se vio afectada por la falla | **Numerador**: solicitudes con `es_correcta = False` cuya respuesta cae entre `falla_inyectada` y el `cambio_estado` a `unhealthy`. **Denominador**: **total** de solicitudes de tráfico sostenido de la corrida completa (~1000), **no** las de la ventana | **< 2 %** |
| **Tiempo de reintegración** (`tiempo_reintegracion_s`) | Cuánto tardó el Monitor en volver a marcar `instancia-3` como `healthy` tras reparar la falla | `timestamp` del `cambio_estado` a `healthy` − `timestamp` del `falla_limpiada` del harness | **≤ 20 s** |
| **Tasa de falsos positivos** (`falsos_positivos_pct`) | Si el Monitor marcó como fallidas instancias que estaban sanas (`instancia-1` e `instancia-2`, que nunca reciben falla) | % de `resultado_probe` con `resultado = "fallo"` sobre el total de probes de esas dos instancias en la ventana de la corrida | **0 %** |

**Por qué el denominador de la ventana de exposición es el total de la corrida y no
el de la ventana:** si fuera solo la ventana, el porcentaje de solicitudes erróneas
sería alto casi por construcción (una fracción del tráfico de esos segundos siempre
cae en la instancia con la falla vía el round-robin del Router), y el umbral de 2 %
dejaría de ser informativo. Con el total de la corrida como denominador, la métrica
expresa **qué parte de la experiencia completa del cliente** (no solo durante la
falla) se degradó — comparable en espíritu a un indicador de disponibilidad.

### 7.2 `resultados_consolidados.csv` — columna por columna

| Columna | Significado |
| --- | --- |
| `tipo_falla` | `timeout` \| `error_http` \| `prima_inconsistente` |
| `numero_corrida` | 1 a 20 dentro de ese tipo |
| `tiempo_deteccion_s` | Métrica 1. Vacío = el Monitor **nunca** detectó la falla en esa corrida (dato relevante, no un simple hueco) |
| `ventana_exposicion_pct` | Métrica 2, en % |
| `solicitudes_incorrectas_ventana` | Numerador de la métrica 2 (conteo bruto) |
| `solicitudes_totales_corrida` | Denominador de la métrica 2 (tráfico sostenido de la corrida) |
| `tiempo_reintegracion_s` | Métrica 3. Vacío = no se observó la reintegración dentro de la corrida |
| `falsos_positivos_pct` | Métrica 4, en % |
| `cumple_deteccion` | `True` si `tiempo_deteccion_s ≤ 15` |
| `cumple_exposicion` | `True` si `ventana_exposicion_pct < 2` |
| `cumple_reintegracion` | `True` si `tiempo_reintegracion_s ≤ 20` |
| `nota` | Texto si la corrida quedó incompleta (p. ej. falta `inicio_corrida` / `fin_corrida`) |

Lectura sugerida: ordenar por `tipo_falla` y revisar las columnas `cumple_*`. Una
columna con casi todo `True` respalda esa métrica para ese tipo de falla; `False`
dispersos invitan a abrir el log crudo de esas corridas concretas.

### 7.3 `resumen_por_tipo_falla.csv`

Tres filas (una por tipo de falla). Para cada métrica trae media, mediana, p95,
mínimo, máximo y `pct_cumple_*` (porcentaje de las 20 corridas que cumplieron el
umbral). El **p95** y el **máximo** importan tanto como la media: un promedio bajo
con un p95 por encima del umbral significa que el diseño falla en la cola, no de
forma uniforme.

### 7.4 Los tres boxplots

Cada caja resume las 20 corridas de un tipo de falla; la línea roja es el umbral.
Interpretación rápida:

- **Caja completa por debajo de la línea** → el diseño cumple esa métrica para ese
  tipo de falla de forma consistente.
- **Bigote superior o *outliers* por encima de la línea** → cumple "en promedio"
  pero no siempre; hay que mirar las corridas concretas.
- **Caja cruzando o por encima de la línea** → no cumple.

### 7.5 El veredicto de consola

`analizar_resultados.py` imprime, por tipo de falla, el % de corridas que cumple
cada umbral y una etiqueta **HIPÓTESIS RESPALDADA / RECHAZADA** con criterio
automático de **≥ 95 % de las 20 corridas** cumpliendo las tres métricas.

Es **solo un apoyo de lectura rápida.** La decisión final —si un umbral "se cumple
de forma consistente" y no solo en promedio— es del análisis, revisando los datos
reales, no de este script.

### 7.6 Criterio de aceptación / rechazo de la hipótesis

- **Se respalda**, para un tipo de falla, si las 20 corridas cumplen de forma
  consistente (no en una corrida aislada): detección ≤ 15 s, exposición < 2 % y
  reintegración ≤ 20 s.
- **Se rechaza, total o parcialmente**, si algún criterio falla de forma sistemática
  (no ocasional) para uno o más tipos de falla.
- **Atención especial a `prima_inconsistente`:** si no se detecta dentro del umbral,
  o no se detecta, es evidencia de que el chequeo semántico no cumple su función.

### 7.8 Cómo reconstruir una corrida concreta desde los logs crudos

1. Abrir `harness-corrida-{tipo}-{n}.jsonl`. Tomar el `timestamp` de `inicio_corrida`
   y el de `fin_corrida`: definen la ventana temporal de la corrida.
2. Filtrar `ms-monitor.jsonl` y `ms-router.jsonl` a ese rango de `timestamp`.
3. Ubicar en el harness los eventos `falla_inyectada` (t≈20 s) y `falla_limpiada`
   (t≈60 s).
4. Buscar en el monitor el primer `cambio_estado` de `instancia-3` a `unhealthy`
   después de `falla_inyectada` (→ tiempo de detección) y el primer `cambio_estado`
   a `healthy` después de `falla_limpiada` (→ tiempo de reintegración).
5. Contar en el harness los `solicitud_resultado` con `es_correcta = False` entre
   `falla_inyectada` y ese `cambio_estado` a `unhealthy` (→ numerador de la ventana
   de exposición).
6. Para entender el enmascaramiento, revisar en `ms-router.jsonl` los
   `solicitud_enrutada`: tras la detección, `instancia_elegida` no debe volver a ser
   `instancia-3` hasta después de la reintegración.

---

## 8. Notas de integración

- `INSTANCIAS`, `REDIS_HOST`, `REDIS_PORT`, `INTERVALO_CICLO_SEGUNDOS`,
  `TIMEOUT_PROBE_SEGUNDOS`, `CICLOS_FALLIDOS_PARA_NO_SALUDABLE`,
  `CICLOS_EXITOSOS_PARA_REINTEGRAR` y `TIMEOUT_FORWARD_SEGUNDOS` viven en `.env` y
  las comparten `ms-monitor` y `ms-router` vía `env_file`.
- Timeouts de cliente HTTP: Monitor → Cotización **3 s**; Router → Cotización
  **3 s**; harness → Router **5 s** (mayor que el del Router para recibir siempre una
  respuesta —exitosa o de error— antes de rendirse).
- Los `Dockerfile` de `ms-cotizacion`, `ms-router` y `ms-monitor` instalan `curl`
  para los `healthcheck` HTTP de Compose.
- El `healthcheck` de `ms-router` solo confirma que el puerto 8000 responde; **no**
  envía el payload real de `/cotizar`, para no inyectar tráfico sintético extra
  durante las 60 corridas.
- Los endpoints `POST /inject-fault` y `POST /clear-fault` existen **solo** para el
  experimento y solo en `ms-cotizacion`; no hacen parte del diseño real.

