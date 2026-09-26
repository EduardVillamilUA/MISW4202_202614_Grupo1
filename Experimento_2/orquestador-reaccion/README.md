# orquestador-reaccion

Se suscribe de forma continua a los hallazgos de patrones de acceso indebido y, por
cada uno, ejecuta la reacción de seguridad correspondiente — bloquear al actor que lo
originó — de forma **idempotente**, dejando constancia en el Registro de Auditoría
tanto del hallazgo recibido como de la reacción efectivamente ejecutada.

**El punto de diseño central es que la reacción de seguridad nunca depende de que la
auditoría se pueda escribir.** Si el Registro de Auditoría está lento o caído, el
bloqueo del actor se ejecuta de todas formas; lo único que se pierde es la constancia
escrita de ese hecho (que queda, en su lugar, en el log local del contenedor).

Este componente no decide si una consulta es indebida — solo reacciona ante hallazgos
que otro componente ya determinó. Tampoco expone su endpoint público al tráfico normal
del experimento: sus dos endpoints HTTP son exclusivamente de diagnóstico.

---

## 1. Cómo funciona

El proceso hace dos cosas en paralelo:

- Un **hilo de fondo** se suscribe indefinidamente al canal de hallazgos y procesa cada
  mensaje que llega. Es el corazón del componente.
- El **servidor Flask** solo atiende `GET /bloqueados` y `GET /salud`, para que el
  harness del experimento pueda verificar el estado del sistema sin tener que leer
  Redis directamente.

Por cada hallazgo recibido en el canal, se ejecuta esta secuencia:

1. **Se registra el hallazgo tal como llegó**, antes de actuar sobre él (`tipo_registro
   = "hallazgo"`, sin fecha de reacción todavía). Esto permite medir por separado,
   más adelante, cuánto tiempo pasó entre la detección y la reacción.
2. **Se verifica idempotencia**: se intenta escribir una clave de deduplicación propia
   de este `evento_id` con una operación que solo tiene éxito si la clave no existía
   todavía.
   - Si la clave **ya existía** → el evento es una entrega duplicada del mismo
     hallazgo. Se registra una segunda entrada de tipo `reaccion` con
     `es_duplicado = true` y **no se repite la acción de bloqueo**. El proceso termina
     aquí para este mensaje.
   - Si la clave **no existía** → es la primera vez que se ve este `evento_id`, se
     continúa con el paso 3.
3. **Se ejecuta la reacción real**: se agrega al actor al conjunto de actores
   bloqueados. En este experimento no existe un autorizador real que emita o revoque
   credenciales, así que "revocar credencial" y "bloquear al actor" se modelan como la
   misma acción observable: pertenecer a ese conjunto es, en la práctica, lo único que
   hace que el resto del sistema deje de atenderlo.
4. **Se registra la reacción ya ejecutada** (`tipo_registro = "reaccion"`,
   `es_duplicado = false`, con la fecha en que se ejecutó el bloqueo).

### Por qué el chequeo de idempotencia es explícito, y no solo implícito

Agregar el mismo actor dos veces al conjunto de bloqueados no cambia el resultado final
por sí solo: esa operación ya es idempotente por naturaleza. Pero eso no basta para
**demostrar** que el sistema efectivamente reconoce y descarta entregas duplicadas — un
resultado final correcto podría deberse tanto a que el mecanismo de deduplicación
funcionó como a que, por las propiedades de esa operación, daba igual que no
funcionara. Por eso existe una clave de deduplicación explícita por `evento_id`, con
expiración configurable: permite dejar evidencia registrada de cuántos duplicados
llegaron y de que fueron identificados como tales antes de decidir no repetir la
acción, en vez de asumirlo.

### Por qué la auditoría se reintenta pero la reacción nunca espera por ella

Cada llamada al Registro de Auditoría se reintenta una vez, tras una espera corta, si
falla. Si el segundo intento también falla, el intento se abandona y el payload
completo queda en el log local del contenedor (para poder reconstruirlo manualmente si
hiciera falta), pero **eso ocurre en paralelo, sin bloquear ni condicionar el paso 3**.
La razón es que este componente ejecuta una acción de seguridad; postergarla o
cancelarla porque un servicio de auditoría —secundario respecto a esa acción— no
respondió sería priorizar mal las dos responsabilidades del componente.

### Por qué el hilo de fondo se reconecta en vez de terminar

La suscripción a Redis debe durar toda la vida del proceso, no el ciclo de una
solicitud HTTP. Si la conexión con Redis se cae, el hilo no se da por vencido: espera
un momento y vuelve a suscribirse. Un error al procesar un mensaje puntual (por
ejemplo, JSON malformado) tampoco tumba la suscripción completa: se registra en el log
y se sigue escuchando el resto de los mensajes.

## 2. Tecnología

- Python 3.11 + Flask, solo para los dos endpoints de diagnóstico; la lógica principal
  no pasa por Flask en ningún momento.
- `redis` (cliente `redis-py`), usando `pubsub()` para la suscripción continua al canal
  de hallazgos, y comandos simples (`SET ... NX EX`, `SADD`, `SMEMBERS`) para la
  deduplicación, el bloqueo y la lectura de diagnóstico.
- `requests` para llamar al Registro de Auditoría.
- Un hilo (`threading.Thread`, `daemon=True`) dedicado exclusivamente al consumo del
  canal, arrancado al importar el módulo, en paralelo al servidor Flask.

## 3. Configuración (variables de entorno)

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `URL_REGISTRO_AUDITORIA` | `http://registro-auditoria:8004` | Dirección interna del Registro de Auditoría |
| `TTL_PROCESADOS_SEGUNDOS` | `3600` | Expiración de las claves de deduplicación por `evento_id` |
| `PUERTO` | `8003` | Puerto en el que escucha Flask dentro del contenedor |

El nombre del canal de suscripción (`canal:hallazgos`), el timeout y la política de
reintento hacia el Registro de Auditoría, y la espera antes de reconectar a Redis están
fijados en el código: son detalles internos de este componente, no valores que otro
componente necesite conocer o configurar.

## 4. Endpoints

### `GET /bloqueados`

Devuelve la lista actual de actores bloqueados (`SMEMBERS bloqueados`, ordenada
alfabéticamente):

```bash
curl http://localhost:8003/bloqueados
```

```json
{ "actores_bloqueados": ["actor-ase-001", "actor-cli-002"] }
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | `{"actores_bloqueados": [...]}` | Lectura exitosa |
| `503` | `{"error": "redis_no_disponible"}` | Redis no respondió a la lectura |

### `GET /salud`

```bash
curl http://localhost:8003/salud
```

Verifica, en este orden, que Redis responda a `PING`, que el hilo consumidor esté
efectivamente suscrito al canal de hallazgos, y que el Registro de Auditoría responda a
su propio `GET /salud`:

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | `{"estado": "ok"}` | Las tres verificaciones pasaron |
| `503` | `{"estado": "redis_no_disponible"}` | Redis no responde |
| `503` | `{"estado": "consumidor_hallazgos_inactivo"}` | El hilo de fondo no está suscrito (por ejemplo, reconectando) |
| `503` | `{"estado": "registro_auditoria_no_disponible"}` | El Registro de Auditoría no respondió |

## 5. Manejo de errores hacia el Registro de Auditoría

Si `POST /eventos` falla (servicio no disponible, timeout, error `5xx`):

1. Se espera un instante corto y se reintenta una vez.
2. Si el segundo intento también falla, se registra en el log local del contenedor el
   payload completo que no se pudo persistir, junto con el error, para poder
   reconstruirlo manualmente si hace falta.
3. En ningún caso este fallo impide ni retrasa la ejecución del bloqueo del actor: la
   acción de seguridad y el registro de auditoría son responsabilidades independientes.

## 6. Ejecución

### Con Docker

```bash
docker build -t orquestador-reaccion .
docker run --rm -p 8003:8003 \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  -e URL_REGISTRO_AUDITORIA=http://registro-auditoria:8004 \
  -e TTL_PROCESADOS_SEGUNDOS=3600 \
  -e PUERTO=8003 \
  orquestador-reaccion
```

Necesita Redis accesible por red desde el arranque (el hilo de fondo intenta
suscribirse de inmediato) y, para que la auditoría se registre, también
`registro-auditoria` accesible — aunque, como se explica en la sección 5, su ausencia
no impide que el componente cumpla su función de bloqueo.

### En local (Python)

```bash
pip install -r requirements.txt
REDIS_HOST=localhost REDIS_PORT=6379 \
URL_REGISTRO_AUDITORIA=http://localhost:8004 \
PUERTO=8003 \
python app.py
```

## 7. Casos de prueba verificados antes de integrar

1. Publicar manualmente un hallazgo nuevo en `canal:hallazgos` (con `redis-cli PUBLISH`
   o desde otro componente) → se registra el `hallazgo`, se ejecuta `SADD`, se registra
   la `reaccion` con `es_duplicado = false`, y el actor aparece en `GET /bloqueados`.
2. Publicar el mismo `evento_id` una segunda vez → se registra un segundo `hallazgo`,
   pero la `reaccion` correspondiente llega con `es_duplicado = true`, y no se dispara
   ninguna acción de bloqueo adicional más allá de la que ya ocurrió con la primera
   entrega.
3. Bloquear al mismo actor a través de dos hallazgos con `evento_id` distintos →
   `GET /bloqueados` lo muestra una sola vez (el conjunto de Redis no admite
   duplicados).
4. Apagar temporalmente `registro-auditoria`, publicar un hallazgo, y confirmar que el
   actor igual queda bloqueado (`SADD` se ejecuta) aunque la escritura de auditoría
   falle tras los dos intentos y quede solo en el log.
5. Publicar un mensaje malformado (JSON inválido o sin alguno de los campos
   requeridos) en el canal → se registra el error en el log y el hilo consumidor sigue
   funcionando con normalidad para el siguiente mensaje.
6. Medir el tiempo entre `timestamp_deteccion` del hallazgo y el momento en que
   `SADD bloqueados` se ejecuta (visible en el log del contenedor), como insumo para la
   latencia de reacción.

```bash
# Publicar un hallazgo de prueba manualmente
redis-cli PUBLISH canal:hallazgos '{
  "evento_id": "9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
  "evento_origen_id": "3f2a9e2e-1b3a-4b8b-9b9a-6b9e8f2c1a01",
  "actor_id": "actor-ase-001",
  "client_id_consultado": "cli-045",
  "tipo_deteccion": "heuristico",
  "razon": "patron_diversidad",
  "timestamp_evento_origen": "2026-09-21T15:04:32.118Z",
  "timestamp_deteccion": "2026-09-21T15:04:32.401Z"
}'

curl http://localhost:8003/bloqueados
```

## 8. Integración con el resto del experimento

Este componente no genera tráfico por sí mismo: reacciona ante lo que otro componente
publica en `canal:hallazgos` después de analizar patrones de acceso. Para que tenga
algo que procesar, ese componente detector debe estar activo y publicando en ese canal.
El conjunto de actores bloqueados que este componente escribe en Redis es leído, antes
de cada solicitud, por el punto de entrada del sistema — así es como una reacción
decidida aquí termina afectando el tráfico real. El Registro de Auditoría, por su
parte, es quien le da a este componente un lugar donde dejar constancia verificable de
cada hallazgo y cada reacción, insumo necesario para calcular las métricas de latencia
y de manejo de duplicados al final de cada corrida.
