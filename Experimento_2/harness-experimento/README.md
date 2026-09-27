# harness-experimento

Conjunto de scripts en Python que actúan como **cliente** del resto del sistema (a través de
`api-gateway` y de `registro-auditoria`) para generar tráfico simulado, inyectar patrones de ataque
controlados, y calcular las métricas del experimento. No es un microservicio: no se despliega en
Docker Compose junto con los demás componentes ni expone ningún puerto propio. Es el instrumento de
medición, no el objeto medido.

**No decide nada sobre si un acceso es indebido.** Esa lógica vive por completo en
`monitor-accesos-indebidos` y en `orquestador-reaccion`. El harness solo sabe qué solicitudes envió y en
qué instante, y compara eso contra lo que el sistema terminó registrando en `registro-auditoria`. Esa
comparación es lo único que permite calcular falsos negativos: algo que el sistema debió detectar y no
detectó nunca aparece en el registro de auditoría, así que la única forma de saber que ocurrió es
teniendo, por fuera del sistema, la constancia independiente de que la solicitud se envió.

---

## 1. Cómo funciona

### 1.1. Componentes del harness

| Script | Rol |
| --- | --- |
| `comun.py` | Utilidades compartidas: carga del directorio de actores, formato de timestamps, cliente HTTP hacia `api-gateway`, escritura del registro local de solicitudes. No es ejecutable. |
| `seed_actores.py` | Genera `config/actores.json` (el directorio estático que lee `api-gateway`) con una mezcla configurable de roles. |
| `generador_trafico_legitimo.py` | Simula tráfico normal: clientes consultando su propio perfil, asesores/operaciones consultando un puñado de perfiles distintos. |
| `generador_ataque.py` | Inyecta, de forma puntual, una violación de alcance o una ráfaga de diversidad. |
| `ejecutar_combinacion.py` | Orquesta una corrida completa: lanza tráfico legítimo, inyecta el ataque en el instante correcto, y recupera los eventos de auditoría al terminar. |
| `analizar_resultados.py` | Calcula las métricas y genera las tablas y gráficas a partir de los archivos que produjo `ejecutar_combinacion.py`. |
| `analizar_calibracion.py` | Calcula la tasa de falsos positivos de una corrida generada directamente con `generador_trafico_legitimo.py` (sin pasar por `ejecutar_combinacion.py`), cruzando el `.jsonl` crudo de solicitudes contra `GET /eventos` de `registro-auditoria` en vivo. Pensado específicamente para la fase de calibración del umbral heurístico, antes de fijarlo para las corridas formales. |
| `combinaciones.json` | Define, por número de combinación, qué mezcla de tráfico y qué ataque (si aplica) ejecuta `ejecutar_combinacion.py`. |

### 1.2. Generación de tráfico legítimo

Cada actor que participa en una corrida corre en su propio hilo, con su propio reloj — no hay un único
proceso central emitiendo solicitudes a una tasa fija, porque lo que se quiere imitar es el comportamiento
de varias personas usando el sistema de forma independiente:

- **Actores `cliente`**: consultan únicamente su propio `client_id_propio`, a intervalos aleatorios entre
  2 y 15 segundos.
- **Actores `asesor`/`operaciones`**: en ciclos sucesivos, eligen entre 2 y 4 clientes distintos al azar (de
  un pool configurable de perfiles simulados) y los consultan repartidos a lo largo de una ventana de
  referencia (30 segundos por defecto), con algo de aleatoriedad para no producir un patrón artificialmente
  uniforme.

Los parámetros `--proporcion-clientes`, `--proporcion-asesores` y `--proporcion-operaciones` deciden qué
fracción de los actores disponibles de cada rol participa en una corrida dada, para poder variar la mezcla
de tráfico sin editar el directorio de actores. El parámetro `--tasa-promedio-por-segundo` es informativo
(se usa para loguear una tasa estimada de referencia): la cadencia real de cada actor sigue los rangos por
rol descritos arriba, que ya están pensados para no disparar la regla heurística de detección si los
umbrales del monitor están bien calibrados.

El rango de 2 a 4 clientes distintos por ventana de los actores de rol ampliado también se puede ampliar con
`--diversidad-min-por-ventana` y `--diversidad-max-por-ventana` (por defecto, 2 y 4 — el comportamiento
original queda intacto si no se pasan). Esto se agregó para poder generar, durante la fase de calibración,
tráfico legítimo deliberadamente más exigente que el normal y así distinguir candidatos de `UMBRAL_DIVERSIDAD`
que con el tráfico por defecto resultan indistinguibles.

### 1.3. Inyección de ataques

`generador_ataque.py` reproduce, de forma puntual y controlada, los dos patrones de acceso indebido que el
resto del sistema debe detectar:

- **`violacion_alcance`**: un actor `cliente` consulta un `client_id` que no es el suyo, en una única
  solicitud.
- **`rafaga_diversidad`**: un actor `asesor`/`operaciones` consulta, en una ráfaga concentrada en pocos
  segundos, varios clientes distintos. Si se le indica `--url-monitor`, el script consulta primero
  `GET /estado` de `monitor-accesos-indebidos` para leer el `umbral_diversidad` realmente vigente y calcula
  el tamaño de la ráfaga como ese umbral más un margen — así el ataque queda calibrado automáticamente
  incluso durante una corrida de calibración que cambia el umbral entre repeticiones. Si el monitor no
  responde, se usa un valor por defecto fijo.

### 1.4. Orquestación de una corrida (`ejecutar_combinacion.py`)

1. Registra el `timestamp_inicio_corrida`.
2. Lanza `generador_trafico_legitimo` en un hilo de fondo durante toda la duración configurada.
3. Si la combinación incluye un ataque, espera hasta el instante configurado (una fracción de la duración
   total, para que quede rodeado de tráfico legítimo) y lo inyecta.
4. Espera un margen adicional tras el fin de la duración configurada, para dar tiempo a que cualquier
   detección y reacción en curso termine.
5. Consulta `GET /eventos` de `registro-auditoria`, filtrando por el rango de tiempo de la corrida.
6. Combina, en un único archivo de salida, el registro local de solicitudes enviadas y los eventos
   recuperados de `registro-auditoria`.

Las combinaciones mismas (duración, mezcla de roles, qué ataque inyectar y en qué momento) se definen en
`combinaciones.json`, no en el script — así se pueden agregar o ajustar combinaciones de prueba sin tocar
código. El repositorio incluye tres combinaciones de ejemplo (ver sección 4).

#### Simulación de entrega duplicada

Para poder validar que `orquestador-reaccion` no ejecuta dos veces la acción de bloqueo cuando el mismo
hallazgo llega repetido, la combinación puede marcar `"simular_entrega_duplicada": true`. Cuando eso ocurre,
justo antes de inyectar el ataque el orquestador de la corrida se suscribe por su cuenta a
`canal:hallazgos`, espera a que aparezca el primer hallazgo correspondiente al actor atacado, y vuelve a
publicar exactamente el mismo mensaje (mismo `evento_id` incluido) una segunda vez. Esto reproduce, de
forma controlada, el escenario de una entrega duplicada por el bus de mensajería. Se usa el propio actor
atacado como filtro para no confundir este hallazgo con el de otro ataque que pudiera estar corriendo en
paralelo; el actor a usar para el ataque se resuelve **antes** de inyectarlo (por defecto uno fijo del
directorio, o el indicado en `--actor-ataque`), precisamente para que el hilo que escucha el duplicado sepa
de antemano a quién debe filtrar.

### 1.5. Cálculo de métricas (`analizar_resultados.py`)

Lee uno o varios archivos combinados y calcula, por corrida y agregado por combinación:

| Métrica | Cómo se calcula |
| --- | --- |
| Tasa de detección | De las solicitudes marcadas como ataque, proporción para las que existe un hallazgo en el registro de auditoría con el mismo `actor_id`/`client_id_consultado`, detectado entre 0 y `--ventana-correlacion-segundos` después del envío. |
| Tasa de falsos positivos | Igual, pero sobre las solicitudes marcadas como tráfico legítimo. |
| Latencia de detección (p95) | `timestamp_deteccion − timestamp_envio` de cada ataque detectado; percentil 95 sobre la corrida. |
| Latencia de reacción (p95) | `timestamp_reaccion − timestamp_deteccion` de la reacción no-duplicada asociada a cada hallazgo; percentil 95 sobre la corrida. |
| Tasa de reacciones idempotentes correctas | Se agrupan las reacciones por `evento_id`; para cada grupo con más de una reacción (indicio de hallazgo duplicado), se confirma que a lo sumo una está marcada `es_duplicado=false` — eso es, mirando el propio registro, la prueba de que la acción de bloqueo real solo se ejecutó una vez. |

La tabla agregada por combinación muestra el promedio y el percentil 95 de cada métrica sobre las
repeticiones de esa combinación, tal como se necesita para el informe final. También se generan dos
gráficas: tasa de falsos positivos contra el umbral bajo prueba (requiere que se haya pasado
`--umbral-bajo-prueba` a `ejecutar_combinacion.py` en cada corrida de calibración, porque este script no
tiene forma de saber qué umbral estaba activo en el monitor durante una corrida ya terminada) y la
distribución de latencias de detección de las combinaciones 2 y 3.

## 2. Tecnología

- Python 3.11 o 3.12. En Windows, evitar Python 3.13/3.14: `pandas==2.2.2` no publica wheels
  precompilados para esas versiones, así que `pip` cae a compilarlo desde el código fuente y falla
  si no hay Visual Studio Build Tools instalados (error típico: `Could not find ... vswhere.exe`
  durante `Preparing metadata (pyproject.toml)`). Ver sección 1 para cómo preparar un entorno
  virtual con una versión compatible.
- `requests` para las llamadas HTTP a `api-gateway`, `monitor-accesos-indebidos` (diagnóstico) y
  `registro-auditoria`.
- `redis` (cliente `redis-py`), usado únicamente por `ejecutar_combinacion.py` para la simulación de
  entrega duplicada descrita en 1.4 — es el único punto del harness que habla con Redis directamente; todo
  lo demás pasa por las APIs HTTP de los servicios.
- `pandas` y `matplotlib` para el cálculo de métricas y la generación de tablas/gráficas.
- Sin imagen propia de Docker: se ejecuta directamente desde el host (o desde cualquier entorno con Python
  3.11/3.12), que es justo lo que necesita para actuar como cliente externo del resto del sistema.

## 3. Formato de datos

### 3.1. Registro local de una solicitud

Cada solicitud enviada por `generador_trafico_legitimo.py` o `generador_ataque.py` se registra como una
línea JSON:

```json
{
  "actor_id": "actor-ase-001",
  "client_id_consultado": "cli-045",
  "timestamp_envio": "2026-09-21T15:04:32.100Z",
  "timestamp_respuesta": "2026-09-21T15:04:32.150Z",
  "codigo_http": 200,
  "es_trafico_legitimo": false,
  "es_ataque_inyectado": true,
  "patron_ataque": "rafaga_diversidad"
}
```

`codigo_http` es `null` cuando la solicitud falló por un error de red (timeout, conexión rechazada): ese
resultado también se registra, en vez de descartarse.

### 3.2. Archivo combinado de una corrida (salida de `ejecutar_combinacion.py`)

Un único archivo JSON Lines con tres tipos de línea, distinguibles por `tipo_linea`:

- `"metadata"`: una línea al inicio del archivo, con la combinación, la repetición, los parámetros usados
  y los timestamps de inicio/fin de la corrida.
- `"solicitud"`: una línea por cada solicitud enviada (mismo formato que 3.1, más el campo `tipo_linea`).
- `"evento_auditoria"`: una línea por cada registro recuperado de `GET /eventos` en `registro-auditoria`.

Este es el archivo que consume `analizar_resultados.py`.

## 4. Configuración

### 4.1. Parámetros comunes (línea de comandos)

| Parámetro | Presente en | Valor por defecto | Descripción |
| --- | --- | --- | --- |
| `--ruta-directorio-actores` | todos salvo `analizar_resultados.py` | `../api-gateway/config/actores.json` | Ruta al mismo directorio de actores que usa `api-gateway`. Se asume ejecución desde este directorio, con `api-gateway/` como carpeta hermana. |
| `--cantidad-perfiles` | generadores y orquestador | `50` | Cuántos perfiles simulados (`cli-001` .. `cli-{N:03d}`) hay disponibles para elegir como objetivo; debe coincidir con lo sembrado por `seed_perfiles.py` en `servicio-perfilamiento`. |
| `--url-gateway` | generadores y orquestador | `http://localhost:8000` | URL pública de `api-gateway`. |
| `--url-monitor` | `generador_ataque.py`, `ejecutar_combinacion.py` | `http://localhost:8002` | URL de diagnóstico de `monitor-accesos-indebidos`, usada para autocalibrar la ráfaga de diversidad. |
| `--url-registro-auditoria` | `ejecutar_combinacion.py` | `http://localhost:8004` | URL de `registro-auditoria`. |
| `--redis-host` / `--redis-port` | `ejecutar_combinacion.py` | `localhost` / `6379` | Conexión directa a Redis, solo para la simulación de entrega duplicada. |

### 4.2. `combinaciones.json`

Cada entrada define una combinación de prueba:

```json
{
  "2": {
    "descripcion": "Tráfico legítimo + una violación de alcance inyectada a mitad de la corrida.",
    "duracion_segundos": 120,
    "tasa_promedio_por_segundo": 1.0,
    "proporcion_clientes": 0.5,
    "proporcion_asesores": 0.35,
    "proporcion_operaciones": 0.15,
    "patron_ataque": "violacion_alcance",
    "instante_inyeccion_fraccion": 0.5,
    "simular_entrega_duplicada": false
  }
}
```

El repositorio incluye tres combinaciones de ejemplo: la `1` es una prueba de humo con solo tráfico
legítimo, la `2` inyecta una violación de alcance, y la `3` inyecta una ráfaga de diversidad con simulación
de entrega duplicada. Se pueden agregar, quitar o ajustar combinaciones editando este archivo; no hace
falta modificar ningún script.

Los umbrales de detección (`UMBRAL_VOLUMEN`, `UMBRAL_DIVERSIDAD`, `VENTANA_SEGUNDOS`) **no** se configuran
desde aquí: son variables de entorno de `monitor-accesos-indebidos` y se cambian reiniciando ese contenedor
con un nuevo valor. Durante una corrida de calibración, hay que decirle a `ejecutar_combinacion.py` qué
umbral está activo en ese momento con `--umbral-bajo-prueba`, para que `analizar_resultados.py` pueda
después graficar la tasa de falsos positivos contra ese valor.

## 5. Ejecución

### 5.1. Requisitos previos

- El resto del sistema (`api-gateway`, `servicio-perfilamiento`, `monitor-accesos-indebidos`,
  `orquestador-reaccion`, `registro-auditoria`, Redis) ya desplegado y accesible.
- `config/actores.json` de `api-gateway` poblado (a mano, o con `seed_actores.py`).
- Perfiles simulados sembrados en Redis con `seed_perfiles.py` (al menos tantos como
  `--cantidad-perfiles`).
- Un intérprete de Python 3.11 o 3.12 (ver sección 2 sobre por qué no usar 3.13/3.14 en Windows).

Crear un entorno virtual e instalar las dependencias ahí (una sola vez; en corridas posteriores solo
hace falta reactivarlo):

```bash
# Windows (PowerShell), usando el lanzador py para elegir la versión:
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1

# macOS/Linux:
python3.12 -m venv .venv
source .venv/bin/activate
```

Con el entorno activado:

```bash
pip install -r requirements.txt
```

### 5.2. Preparar actores y perfiles (una sola vez, o cada vez que se quiera reiniciar la corrida)

```bash
python seed_actores.py --num-clientes 10 --num-asesores 4 --num-operaciones 2
# desde servicio-perfilamiento:
#   python seed_perfiles.py --cantidad 50
```

### 5.3. Probar un generador de forma aislada

```bash
# Tráfico legítimo durante un minuto
python generador_trafico_legitimo.py --duracion-segundos 60 --salida resultados/trafico.jsonl

# Un único ataque de violación de alcance
python generador_ataque.py --patron violacion_alcance --salida resultados/ataque.jsonl

# Una ráfaga de diversidad, autocalibrada contra el monitor
python generador_ataque.py --patron rafaga_diversidad --url-monitor http://localhost:8002 --salida resultados/ataque.jsonl
```

### 5.4. Ejecutar una combinación completa

```bash
python ejecutar_combinacion.py --combinacion 2 --repeticion 7 --salida resultados/comb2_rep7.jsonl
```

Para una corrida de calibración, indicando el umbral activo en el monitor en ese momento:

```bash
python ejecutar_combinacion.py --combinacion 3 --repeticion 1 \
  --salida resultados/calibracion_umbral5_rep1.jsonl --umbral-bajo-prueba 5
```

### 5.5. Analizar resultados

```bash
python analizar_resultados.py resultados/comb2_rep*.jsonl resultados/comb3_rep*.jsonl
```

Produce `resultados/metricas_por_corrida.csv`, `resultados/metricas_por_combinacion.csv` y las gráficas en
`resultados/graficas/`.

## 6. Casos de prueba verificados antes de integrar

1. `generador_trafico_legitimo.py` durante un minuto contra un entorno ya integrado, confirmando por
   `GET /eventos?tipo_registro=hallazgo` en `registro-auditoria` que no se genera ningún hallazgo con los
   umbrales por defecto.
2. `generador_ataque.py --patron violacion_alcance` una sola vez, confirmando que aparece exactamente un
   hallazgo `deterministico`.
3. `generador_ataque.py --patron rafaga_diversidad`, primero con `UMBRAL_DIVERSIDAD` bajo (por ejemplo `2`)
   confirmando que se detecta, y luego con un umbral alto (por ejemplo `50`) confirmando que no se detecta.
4. `analizar_resultados.py` sobre un archivo combinado armado a mano (sin correr el sistema), verificando
   que las cinco métricas de la sección 1.5 se calculan correctamente contra valores esperados calculados
   manualmente.

## 7. Integración con el resto del experimento

El harness es el único componente que actúa como cliente externo del sistema: envía solicitudes a
`api-gateway` exactamente como lo haría un actor real (mismo encabezado `X-Actor-Id`, mismo endpoint
`GET /perfil/<client_id>`), usa el endpoint de diagnóstico de `monitor-accesos-indebidos` solo para leer
información (nunca para configurarlo), y consulta `registro-auditoria` únicamente por `GET /eventos`. No
escribe en ningún almacén compartido salvo, puntualmente, `canal:hallazgos` cuando simula una entrega
duplicada — y en ese caso republica un mensaje que el propio sistema ya generó, no inventa uno nuevo.

Su salida (los archivos combinados por corrida y las tablas/gráficas de `analizar_resultados.py`) es el
insumo directo para la sección de resultados del informe del experimento; no genera el informe en sí.
