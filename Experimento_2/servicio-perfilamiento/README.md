# servicio-perfilamiento

Resuelve la lectura de un perfil de riesgo simulado y, en paralelo, publica un evento
de auditoría con los datos de esa consulta. Es el productor del evento que otro
componente del sistema consume para analizar patrones de acceso; este servicio no
sabe nada de quién lo consume ni de qué se hace con la información una vez publicada.

**El punto de diseño central es la separación entre el camino síncrono y el
asíncrono.** Responder al solicitante con el perfil (o con el error correspondiente)
nunca debe esperar a que el evento de auditoría termine de publicarse. Si Redis está
lento o momentáneamente inalcanzable en el instante de publicar, el solicitante igual
debe recibir su respuesta a tiempo; el evento simplemente se pierde y queda registrado
en el log, sin afectar la respuesta ya enviada.

Este servicio no realiza llamadas HTTP salientes a ningún otro servicio. Su única
forma de comunicarse hacia el resto del sistema es publicando en un canal de Redis.

---

## 1. Cómo funciona

Para cada `GET /perfil/<client_id>` recibido (siempre reenviado por otro componente
interno, nunca expuesto directamente a clientes externos):

1. Valida que los tres encabezados de identidad del actor solicitante estén presentes
   (`X-Actor-Id`, `X-Actor-Rol`, `X-Actor-Alcance-Propio`). Si falta alguno → `400`, y
   no se publica ningún evento: sin esos datos no hay nada verificable que auditar.
2. Ejecuta `HGETALL perfil:{client_id}` contra Redis.
3. Si el perfil no existe → responde de inmediato `404`. **El evento de acceso se
   publica de todas formas**: una consulta a un perfil inexistente también es un dato
   de interés para el análisis de patrones (por ejemplo, alguien "tanteando" IDs que no
   existen).
4. Si el perfil existe → responde de inmediato `200` con sus datos.
5. Recién después de que la respuesta ya fue devuelta, se construye el evento de acceso
   y se publica en el canal `canal:acceso-perfil` mediante `PUBLISH`. La publicación es
   *fire-and-forget*: no espera confirmación de ningún suscriptor, y si falla (por
   ejemplo, Redis cae justo en ese instante) el error solo se registra en el log; no
   revierte ni afecta la respuesta que el solicitante ya recibió.

Este servicio no reinterpreta ni vuelve a resolver identidad, rol o alcance del actor:
confía en los tres encabezados de entrada tal cual llegan y los repite dentro del
evento que publica.

### Por qué la publicación se delega a un hilo aparte

La publicación se ejecuta en un `ThreadPoolExecutor` de tamaño fijo (8 hilos), no en el
hilo que atiende la solicitud HTTP. Aunque `PUBLISH` de Redis es normalmente una
operación de bajo milisegundo, delegarla a otro hilo elimina por completo la
posibilidad de que una latencia inusual de Redis en ese instante se sume al tiempo de
respuesta que percibe el solicitante. Usar un pool de tamaño fijo (en vez de crear un
hilo nuevo por solicitud) evita que una ráfaga de tráfico dispare una creación
descontrolada de hilos, sin introducir una cola de espera perceptible para el volumen
de tráfico de este experimento.

### Por qué el instante de lectura no es el instante de publicación

El campo `timestamp_lectura` del evento se captura inmediatamente después de resolver
la consulta contra Redis (paso 2), no cuando el hilo de publicación efectivamente lo
envía. Esto importa porque el hilo de publicación puede ejecutarse con un pequeño
desfase respecto al momento real en que se leyó el dato; el evento debe describir
cuándo ocurrió la consulta, no cuándo se terminó de notificar.

## 2. Tecnología

- Python 3.11 + Flask (servidor de desarrollo con `threaded=True`; cada solicitud se
  atiende en su propio hilo, lo cual es suficiente para el volumen de tráfico sintético
  de este experimento).
- `redis` (cliente `redis-py`) tanto para leer perfiles (`HGETALL`) como para publicar
  eventos (`PUBLISH`).
- Sin dependencias HTTP salientes: este servicio no le habla a ningún otro servicio,
  solo a Redis.

## 3. Configuración (variables de entorno)

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `PUERTO` | `8001` | Puerto en el que escucha Flask dentro del contenedor |

El nombre del canal de publicación (`canal:acceso-perfil`) y el tamaño del pool de
hilos de publicación están fijados en el código: son detalles de implementación, no
valores que otro componente necesite configurar externamente.

## 4. Endpoints

### `GET /perfil/<client_id>`

**Encabezados de entrada obligatorios** (enviados por el componente que reenvía la
solicitud):

| Encabezado | Contenido |
| --- | --- |
| `X-Actor-Id` | Identificador del actor solicitante |
| `X-Actor-Rol` | `cliente` \| `asesor` \| `operaciones` |
| `X-Actor-Alcance-Propio` | El `client_id` propio del actor si su rol es `cliente`; cadena vacía en cualquier otro caso |

**Respuestas:**

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | Datos del perfil (`client_id`, `nombre_referencia`, `score_riesgo`, `actualizado_en`) | El perfil existe |
| `404` | `{"error": "perfil_no_encontrado"}` | El perfil no existe (el evento de acceso igual se publica) |
| `400` | `{"error": "encabezados_incompletos"}` | Falta alguno de los tres encabezados de entrada |
| `503` | `{"error": "perfil_no_disponible"}` | Redis no respondió a la lectura del perfil |

En los casos `200` y `404` se publica, de forma asíncrona y después de enviar la
respuesta, un evento en `canal:acceso-perfil` con esta forma:

```json
{
  "evento_id": "3f2a9e2e-1b3a-4b8b-9b9a-6b9e8f2c1a01",
  "actor_id": "actor-ase-001",
  "rol_actor": "asesor",
  "alcance_propio": null,
  "client_id_consultado": "cli-045",
  "timestamp_lectura": "2026-09-21T15:04:32.118Z"
}
```

- `evento_id` es un UUID v4 nuevo por cada consulta.
- `alcance_propio` solo lleva valor cuando `rol_actor` es `cliente`; en cualquier otro
  rol es `null`.
- `timestamp_lectura` es ISO-8601 en UTC con milisegundos.

```bash
curl -i http://localhost:8001/perfil/cli-045 \
  -H "X-Actor-Id: actor-ase-001" \
  -H "X-Actor-Rol: asesor" \
  -H "X-Actor-Alcance-Propio: "
```

### `GET /salud`

```bash
curl http://localhost:8001/salud
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | `{"estado": "ok"}` | El proceso está arriba y Redis responde a `PING` |
| `503` | `{"estado": "redis_no_disponible"}` | Redis no responde |

## 5. Repositorio de perfiles

No existe una base de datos propia: los perfiles viven en el mismo Redis compartido
por el resto del sistema, como claves `perfil:{client_id}` de tipo hash. Este servicio
es el único autorizado a leerlas.

### Siembra de perfiles simulados (`seed_perfiles.py`)

Antes de cualquier corrida hace falta poblar Redis con perfiles de prueba. El script
`seed_perfiles.py`, incluido en este mismo componente, genera al menos 50 perfiles
(`cli-001` a `cli-050` por defecto) con datos simulados:

```json
{
  "client_id": "cli-001",
  "nombre_referencia": "Cliente Simulado 001",
  "score_riesgo": "0.37",
  "actualizado_en": "2026-01-01T00:00:00.000Z"
}
```

Es un script de línea de comandos independiente del proceso Flask: no se ejecuta en
cada arranque del contenedor, sino una vez (o cada vez que se quiera reiniciar el
contenido de los perfiles) antes de una corrida.

**Es idempotente por diseño:** todos los campos de cada perfil se calculan de forma
determinística a partir del índice del perfil (nunca con valores aleatorios ni con la
hora real de ejecución), así que correrlo varias veces siempre escribe exactamente los
mismos datos. `HSET` sobrescribe, nunca duplica ni corrompe.

```bash
# Con el entorno de Python del componente activo
REDIS_HOST=localhost REDIS_PORT=6379 python seed_perfiles.py

# Generando más de los 50 mínimos
REDIS_HOST=localhost REDIS_PORT=6379 python seed_perfiles.py --cantidad 100

# Dentro de un contenedor ya construido, contra la red de Docker Compose
docker run --rm --network <red-compose> \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  servicio-perfilamiento python seed_perfiles.py
```

## 6. Ejecución

### Con Docker

```bash
docker build -t servicio-perfilamiento .
docker run --rm -p 8001:8001 \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  -e PUERTO=8001 \
  servicio-perfilamiento
```

Necesita Redis accesible por red (por nombre de servicio en una red de Docker Compose,
o por `localhost` con el puerto publicado en pruebas locales). No necesita exponerse al
host en una corrida real: solo el componente que reenvía las solicitudes le habla,
dentro de la red interna.

### En local (Python)

```bash
pip install -r requirements.txt
REDIS_HOST=localhost REDIS_PORT=6379 PUERTO=8001 python app.py
```

## 7. Casos de prueba verificados antes de integrar

1. Consulta a un `client_id` existente con los tres encabezados completos → `200` con
   los datos del perfil, y un evento publicado en `canal:acceso-perfil` (verificable
   suscribiéndose manualmente al canal con `redis-cli SUBSCRIBE canal:acceso-perfil`
   mientras se hace la solicitud).
2. Consulta a un `client_id` inexistente → `404`, y el evento de acceso también se
   publica (no se omite por no haber datos que devolver).
3. Solicitud sin alguno de los tres encabezados → `400`, sin publicar ningún evento.
4. Un mensaje malformado publicado manualmente en el canal desde `redis-cli` no afecta
   a este servicio: es responsabilidad exclusiva de quien consume el canal validar lo
   que recibe; el productor no relee ni valida sus propias publicaciones.
5. Medición manual (por ejemplo, cronometrando la respuesta con `curl -w
   "%{time_total}\n"`) confirmando que la latencia percibida por el solicitante no
   varía según Redis tarde más o menos en aceptar el `PUBLISH`, dado que este ocurre en
   un hilo aparte después de haber enviado la respuesta.

```bash
redis-cli SUBSCRIBE canal:acceso-perfil
# en otra terminal:
curl -i http://localhost:8001/perfil/cli-001 \
  -H "X-Actor-Id: actor-cli-001" \
  -H "X-Actor-Rol: cliente" \
  -H "X-Actor-Alcance-Propio: cli-001"
```

## 8. Integración con el resto del experimento

Este servicio no se expone directamente: espera recibir solicitudes ya resueltas
(identidad, rol y alcance del actor) de un componente de entrada que actúa como
gateway. Tampoco decide si una consulta es indebida ni bloquea a nadie; eso ocurre más
adelante, de forma asíncrona, en otro componente que se suscribe al canal
`canal:acceso-perfil`. Para que ese análisis tenga algo que procesar, la instancia de
Redis compartida debe existir y estar accesible antes de levantar este servicio, y el
repositorio de perfiles debe estar sembrado (ver sección 5) antes de generar tráfico de
prueba.
