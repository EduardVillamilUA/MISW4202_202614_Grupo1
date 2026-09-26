# api-gateway

Punto único de entrada síncrono para las consultas de perfil de riesgo. Resuelve la
identidad del actor solicitante contra un directorio estático, verifica que no esté
bloqueado y, si todo está en orden, reenvía la solicitud a `servicio-perfilamiento`
devolviendo la respuesta al cliente tal cual la recibió. Es un *stub* deliberadamente
simple: representa el resultado ya resuelto de autenticación y autorización real (que
se da por sentada en este experimento), no un Gateway de producción.

**No contiene lógica de detección ni de reacción.** Solo aplica bloqueos que ya fueron
decididos por otro componente y resuelve identidad de forma simulada. Adelantar aquí
la evaluación de si una consulta es indebida (por alcance o por patrón de consumo)
sería un error de diseño: esa evaluación ocurre de forma asíncrona, después de que la
solicitud ya fue atendida, precisamente para no penalizar la latencia del camino
síncrono.

---

## 1. Cómo funciona

Para cada `GET /perfil/<client_id>` recibido:

1. Lee el encabezado `X-Actor-Id`. Si falta → `400`.
2. Busca `X-Actor-Id` en el directorio estático de actores, cargado una sola vez en
   memoria al arrancar el proceso. Si el actor no existe → `401`.
3. Consulta en Redis, **sin caché**, si el actor está en el conjunto de bloqueados. Si
   lo está → `403`, sin reenviar nada a `servicio-perfilamiento`.
4. Si pasa ambas verificaciones, reenvía un `GET` a `servicio-perfilamiento` agregando
   tres encabezados internos: el identificador del actor, su rol (resuelto del
   directorio) y su alcance propio (el `client_id` que puede consultar legítimamente
   sin disparar reglas de detección, solo aplica a rol `cliente`).
5. Devuelve al solicitante exactamente el código y el cuerpo que respondió
   `servicio-perfilamiento`, sin transformarlo.
6. Si `servicio-perfilamiento` no responde (timeout o conexión rechazada), responde
   `503` en lugar de propagar una excepción sin controlar.

Cada solicitud registra en el log del proceso (stdout, no en Redis ni en un volumen
compartido) el actor, su rol, el `client_id` consultado y la latencia total, con fines
de depuración y trazabilidad manual.

### Por qué se consulta Redis en cada solicitud, sin caché

Un actor puede pasar de "no bloqueado" a "bloqueado" en cualquier momento, decidido por
otro componente. Cachear ese estado introduciría una ventana en la que el Gateway
seguiría atendiendo a un actor que ya debería estar bloqueado. La consulta
`SISMEMBER` a Redis se resuelve en submilisegundos, así que no cachear no tiene un
costo de rendimiento relevante para el volumen de tráfico de este experimento.

### Por qué el Gateway no decide si una consulta es indebida

Esa responsabilidad es, en su totalidad, de otro componente que actúa después, de
forma asíncrona, analizando el patrón de acceso. El Gateway solo aplica bloqueos ya
decididos. Mantener esta separación es intencional: permite medir la detección de
forma desacoplada del camino síncrono, sin que la detección agregue latencia a cada
solicitud.

## 2. Tecnología

- Python 3.11 + Flask (servidor de desarrollo con `threaded=True`; suficiente para el
  volumen de tráfico sintético de este experimento).
- `redis` (cliente `redis-py`) para consultar el conjunto de actores bloqueados.
- `requests` para reenviar la solicitud a `servicio-perfilamiento`.
- Sin base de datos propia: el directorio de actores es un archivo JSON de solo
  lectura, cargado una única vez al arrancar.

## 3. Configuración (variables de entorno)

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `RUTA_DIRECTORIO_ACTORES` | `/app/config/actores.json` | Ruta del directorio estático de actores |
| `URL_SERVICIO_PERFILAMIENTO` | `http://servicio-perfilamiento:8001` | Dirección interna del servicio de perfilamiento |
| `PUERTO` | `8000` | Puerto en el que escucha Flask dentro del contenedor |

El timeout al reenviar hacia `servicio-perfilamiento` (5 s) está fijado en el código:
es un detalle interno de este componente, no un valor que otro componente necesite
conocer o configurar.

## 4. Directorio estático de actores

Archivo JSON montado como volumen de solo lectura (ruta por defecto
`config/actores.json`), con una entrada por actor:

```json
{
  "actor-cli-001": { "rol": "cliente", "client_id_propio": "cli-001" },
  "actor-ase-001": { "rol": "asesor", "client_id_propio": null }
}
```

- `rol` ∈ `cliente` \| `asesor` \| `operaciones`.
- Solo el rol `cliente` tiene `client_id_propio` no nulo: es el único perfil que ese
  actor puede consultar sin activar la regla de detección de alcance en el resto del
  sistema.
- El archivo se lee **una sola vez** al arrancar el proceso, no en cada solicitud.
- Si el archivo no existe o su contenido no respeta este esquema, el proceso registra
  un error crítico y termina con código de salida distinto de cero: se prefiere que el
  contenedor falle en el arranque a que quede sirviendo tráfico en un estado
  parcialmente funcional.

Este repositorio incluye en `config/actores.json` un directorio de ejemplo con cinco
actores (dos clientes, dos asesores, uno de operaciones) listo para pruebas locales.

## 5. Endpoints

### `GET /perfil/<client_id>`

**Encabezado de entrada obligatorio:** `X-Actor-Id`.

**Encabezados que agrega al reenviar a `servicio-perfilamiento`:**

| Encabezado | Contenido |
| --- | --- |
| `X-Actor-Id` | El mismo valor recibido |
| `X-Actor-Rol` | Rol resuelto del directorio estático |
| `X-Actor-Alcance-Propio` | `client_id_propio` del actor si su rol es `cliente`; cadena vacía en cualquier otro caso |

**Respuestas propias del Gateway** (antes de intentar el reenvío, o si el reenvío
falla):

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `400` | `{"error": "encabezado_actor_faltante"}` | Falta `X-Actor-Id` |
| `401` | `{"error": "actor_no_reconocido"}` | El actor no está en el directorio estático |
| `403` | `{"error": "actor_bloqueado"}` | El actor está en el conjunto de bloqueados de Redis |
| `503` | `{"error": "servicio_perfilamiento_no_disponible"}` | `servicio-perfilamiento` no respondió (timeout o conexión rechazada) |

En cualquier otro caso, el código y el cuerpo son los que devolvió
`servicio-perfilamiento`, propagados sin cambios.

```bash
curl http://localhost:8000/perfil/cli-045 -H "X-Actor-Id: actor-ase-001"
```

### `GET /salud`

```bash
curl http://localhost:8000/salud
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | `{"estado": "ok"}` | El proceso está arriba y Redis responde a `PING` |
| `503` | `{"estado": "redis_no_disponible"}` | Redis no responde |

## 6. Ejecución

### Con Docker

```bash
docker build -t api-gateway .
docker run --rm -p 8000:8000 \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  -e RUTA_DIRECTORIO_ACTORES=/app/config/actores.json \
  -e URL_SERVICIO_PERFILAMIENTO=http://servicio-perfilamiento:8001 \
  -e PUERTO=8000 \
  -v "$(pwd)/config:/app/config:ro" \
  api-gateway
```

Necesita Redis y `servicio-perfilamiento` accesibles por red (por nombre de servicio
en una red de Docker Compose, o por `localhost` con los puertos publicados en pruebas
locales).

### En local (Python)

```bash
pip install -r requirements.txt
REDIS_HOST=localhost REDIS_PORT=6379 \
RUTA_DIRECTORIO_ACTORES=./config/actores.json \
URL_SERVICIO_PERFILAMIENTO=http://localhost:8001 \
PUERTO=8000 \
python app.py
```

## 7. Casos de prueba verificados antes de integrar

1. Actor con rol `cliente` consultando su propio `client_id` → `200`, respuesta del
   perfil.
2. Actor con rol `cliente` consultando un `client_id` ajeno → también `200`: el
   Gateway no bloquea esto de antemano; la detección de esa consulta ocurre después,
   de forma asíncrona, en otro componente.
3. Actor no presente en el directorio → `401`.
4. Actor presente en el conjunto `bloqueados` de Redis → `403`, sin llegar a llamar a
   `servicio-perfilamiento`.
5. `servicio-perfilamiento` apagado o inalcanzable → `503`, sin excepción no
   controlada ni conexión colgada.

```bash
# Preparar un bloqueo de prueba
redis-cli SADD bloqueados actor-ase-001
curl -i http://localhost:8000/perfil/cli-045 -H "X-Actor-Id: actor-ase-001"
# 403

redis-cli SREM bloqueados actor-ase-001
curl -i http://localhost:8000/perfil/cli-045 -H "X-Actor-Id: actor-ase-001"
# 200 si servicio-perfilamiento está arriba y cli-045 existe

curl -i http://localhost:8000/perfil/cli-045 -H "X-Actor-Id: actor-inexistente"
# 401

curl -i http://localhost:8000/perfil/cli-045
# 400 (falta X-Actor-Id)
```

## 8. Integración con el resto del experimento

Este componente es solo el punto de entrada; por sí solo no completa el flujo. Para
una corrida real del experimento necesita, además, un servicio `servicio-perfilamiento`
escuchando en `URL_SERVICIO_PERFILAMIENTO` que resuelva la lectura del perfil a partir
de los tres encabezados `X-Actor-*`, y una instancia de Redis compartida donde otro
componente escriba el conjunto `bloqueados` según vaya decidiendo reacciones. El
Gateway no necesita saber nada de cómo se llega a esas decisiones: solo lee su
resultado final en Redis antes de cada solicitud.
