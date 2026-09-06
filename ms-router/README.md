# ms-router

Fachada única de enrutamiento. Recibe cada solicitud de cotización, consulta el
registro de salud en Redis y **reenvía únicamente a las réplicas marcadas como
`healthy`**, repartiendo la carga entre ellas. Si una réplica deja de estar sana,
el enrutador la excluye automáticamente; cuando vuelve a estar sana, la reincorpora.
No contiene lógica de negocio: solo enruta.

---

## 1. Cómo funciona

Para cada `POST /cotizar` recibido:

1. Genera un `id_solicitud` corto (para trazabilidad en el log; no se envía al
   cliente).
2. **Lee Redis en ese instante, sin caché**, las tres claves
   `solventa:health:instancia-1/2/3`.
3. Arma la lista de réplicas `healthy`. Una clave ausente en Redis se trata como
   **no disponible** (nunca se asume sana).
4. Si **no hay** réplicas sanas → responde `503 {"error": "no hay instancias
   saludables disponibles"}` sin reenviar nada.
5. Si hay una o más → elige una por **round-robin** (turno rotativo) **solo entre las
   sanas del momento**. El contador interno avanza en cada solicitud para repartir la
   carga de forma uniforme.
6. Reenvía el mismo cuerpo JSON a la réplica elegida, con timeout de 3 s.
7. Devuelve la respuesta de la réplica **tal cual** (mismo código, mismo cuerpo),
   **salvo** que el reenvío falle por timeout o conexión rechazada: en ese caso
   responde `502 {"error": "instancia no respondió", "instancia_id": "..."}`.

### Sin caché del estado de salud

El estado se lee de Redis **en cada solicitud**, a propósito. Cachearlo (refrescarlo
cada N segundos) añadiría un retraso extra entre el momento en que el monitor marca
una réplica como no sana y el momento en que el enrutador deja de usarla. Un `GET` a
Redis se resuelve en menos de un milisegundo, así que leerlo siempre no supone un
problema de rendimiento para el volumen de tráfico esperado.

## 2. Tecnología

- Python 3.11 + Flask (servidor de desarrollo con `threaded=True`).
- `redis` (cliente) para leer el registro de salud.
- `requests` para reenviar la solicitud.

## 3. Configuración (variables de entorno)

| Variable | Ejemplo | Descripción |
| --- | --- | --- |
| `INSTANCIAS` | `instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000` | Réplicas disponibles, en formato `id=url_base` separadas por coma. El orden define el orden del round-robin |
| `REDIS_HOST` | `redis` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `TIMEOUT_FORWARD_SEGUNDOS` | `3` | Timeout al reenviar a una réplica |
| `PUERTO` | `8000` | Puerto en el que Flask escucha |
| `ARCHIVO_LOG` | `/resultados/ms-router.jsonl` | Ruta del archivo de eventos (JSON Lines) |

## 4. Endpoint

### `POST /cotizar`

Mismo contrato de entrada y salida que una réplica de `ms-cotizacion`:

```bash
curl -X POST http://localhost:8000/cotizar -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'
```

Respuestas posibles:

| Código | Situación |
| --- | --- |
| `200` | La réplica elegida respondió correctamente. El cuerpo (incluido `instancia_id`) es el de la réplica |
| Código de la réplica (p. ej. `500`) | La réplica respondió con error; el enrutador lo propaga sin cambios |
| `502` | El enrutador no obtuvo respuesta de la réplica (timeout o conexión rechazada) |
| `503` | No había ninguna réplica sana en ese momento |

## 5. Registro de eventos

Cada línea del archivo `ARCHIVO_LOG` es un objeto JSON con `timestamp` (UTC),
`componente: "ms-router"` y `evento`. Eventos:

| `evento` | Cuándo | Campos propios |
| --- | --- | --- |
| `inicio` | Al arrancar | `instancias` |
| `solicitud_recibida` | Al recibir cada `POST /cotizar` | `id_solicitud` |
| `estado_leido` | Justo después de leer Redis | `id_solicitud`, `estados` (estado de las tres réplicas) |
| `solicitud_enrutada` | Tras reenviar (o fallar al reenviar) | `id_solicitud`, `instancia_elegida`, `resultado` (`ok`\|`error_instancia`\|`error_502`), `codigo_http_respuesta`, `latencia_ms` |
| `solicitud_rechazada_sin_instancias` | Cuando no hay réplicas sanas | `id_solicitud` |

## 6. Ejecución

### Con Docker

```bash
docker build -t ms-router .
docker run --rm -p 8000:8000 \
  -e INSTANCIAS=instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000 \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  -e TIMEOUT_FORWARD_SEGUNDOS=3 -e PUERTO=8000 \
  -e ARCHIVO_LOG=/resultados/ms-router.jsonl \
  -v "$(pwd)/resultados:/resultados" \
  ms-router
```

Necesita Redis y al menos una réplica de `ms-cotizacion` accesible por red.

### En local (Python)

```bash
pip install -r requirements.txt
INSTANCIAS=instancia-1=http://localhost:5001,instancia-2=http://localhost:5002,instancia-3=http://localhost:5003 \
REDIS_HOST=localhost REDIS_PORT=6379 \
TIMEOUT_FORWARD_SEGUNDOS=3 PUERTO=8000 \
ARCHIVO_LOG=./ms-router.jsonl \
python app.py
```

## 7. Prueba rápida

```bash
# Marcar las tres réplicas como sanas y pedir una cotización
redis-cli SET solventa:health:instancia-1 healthy
redis-cli SET solventa:health:instancia-2 healthy
redis-cli SET solventa:health:instancia-3 healthy
curl -X POST http://localhost:8000/cotizar -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'
# 200; instancia_id de alguna de las tres, rotando en llamadas sucesivas

# Marcar todas como no sanas
redis-cli SET solventa:health:instancia-1 unhealthy
redis-cli SET solventa:health:instancia-2 unhealthy
redis-cli SET solventa:health:instancia-3 unhealthy
curl -i -X POST http://localhost:8000/cotizar -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'
# 503
```
