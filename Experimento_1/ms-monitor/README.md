# ms-monitor

Monitor de salud. Verifica de forma periódica el estado de las tres réplicas de
`ms-cotizacion` y publica el resultado (`healthy` / `unhealthy`) en un registro
central en Redis, para que `ms-router` pueda dejar de enviar tráfico a una réplica
defectuosa y volver a incluirla cuando se recupera.

Combina **dos verificaciones que operan siempre juntas**:

1. **Chequeo de vida (Ping/Echo):** ¿responde `GET /health` con `200`?
2. **Chequeo semántico:** ¿`POST /cotizar` devuelve `200` **y** la prima esperada?

La segunda es la que detecta fallas de negocio (una réplica que responde `200` pero
calcula mal la prima), invisibles para un chequeo de vida.

---

## 1. Cómo funciona

Es un **proceso de fondo** con un ciclo que se repite cada 5 segundos de forma
indefinida. En cada ciclo, para las tres réplicas y **en paralelo**:

- Lanza los dos chequeos (vida y semántico) con un timeout de 3 s cada uno.
- El ciclo de una réplica es **exitoso solo si ambos chequeos pasan**; es **fallido
  si cualquiera de los dos falla** (timeout, error de conexión, código distinto de
  `200`, o prima distinta de `15000.0` con tolerancia `0.01`).

Los seis chequeos (2 × 3 réplicas) se ejecutan concurrentemente, de modo que el
ciclo completo tarde aproximadamente lo que el chequeo más lento y no la suma de
todos.

### Máquina de estados por réplica (en memoria)

Cada réplica tiene un estado (`healthy` / `unhealthy`, inicia en `healthy`) y dos
contadores:

| Situación | Acción |
| --- | --- |
| Ciclo fallido | `fallos_consecutivos += 1`, `exitos_consecutivos = 0`. Si estaba `healthy` y `fallos_consecutivos >= 2` → pasa a `unhealthy`, escribe en Redis y registra `cambio_estado` |
| Ciclo exitoso | `exitos_consecutivos += 1`, `fallos_consecutivos = 0`. Si estaba `unhealthy` y `exitos_consecutivos >= 3` → vuelve a `healthy`, escribe en Redis y registra `cambio_estado` |

La escritura en Redis ocurre **inmediatamente** al decidir el cambio, para minimizar
el retraso entre la detección y el momento en que el enrutador ve el nuevo estado.
Los contadores no se guardan en Redis: solo importa el resultado final
`healthy` / `unhealthy`.

## 2. Registro de salud en Redis

| Clave | Valor | Notas |
| --- | --- | --- |
| `solventa:health:instancia-1` | `healthy` \| `unhealthy` | Sin expiración (TTL) |
| `solventa:health:instancia-2` | `healthy` \| `unhealthy` | |
| `solventa:health:instancia-3` | `healthy` \| `unhealthy` | |

Al arrancar, las tres claves se inicializan en `healthy` antes de registrar el
evento `inicio`.

## 3. Tecnología

- Python 3.11.
- `redis` (cliente) para escribir el registro de salud.
- `requests` para llamar a `/health` y `/cotizar` de cada réplica.
- Flask, únicamente para el endpoint opcional `GET /status` (también sirve como
  healthcheck del contenedor).

## 4. Configuración (variables de entorno)

| Variable | Ejemplo | Descripción |
| --- | --- | --- |
| `INSTANCIAS` | `instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000` | Réplicas a vigilar, en formato `id=url_base` separadas por coma |
| `REDIS_HOST` | `redis` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `INTERVALO_CICLO_SEGUNDOS` | `5` | Tiempo entre ciclos de verificación |
| `TIMEOUT_PROBE_SEGUNDOS` | `3` | Timeout de cada chequeo HTTP |
| `CICLOS_FALLIDOS_PARA_NO_SALUDABLE` | `2` | Ciclos fallidos consecutivos para marcar `unhealthy` |
| `CICLOS_EXITOSOS_PARA_REINTEGRAR` | `3` | Ciclos exitosos consecutivos para volver a `healthy` |
| `ARCHIVO_LOG` | `/resultados/ms-monitor.jsonl` | Ruta del archivo de eventos (JSON Lines) |
| `PUERTO_DEBUG` | `6000` | Puerto del endpoint `GET /status` |

## 5. Endpoint de depuración

`GET /status` → estado actual en memoria de las tres réplicas. **No** forma parte
del mecanismo de medición; es una ayuda para inspeccionar el monitor a simple vista.

```json
{
  "instancia-1": {"estado": "healthy",   "fallos_consecutivos": 0, "exitos_consecutivos": 4},
  "instancia-2": {"estado": "healthy",   "fallos_consecutivos": 0, "exitos_consecutivos": 4},
  "instancia-3": {"estado": "unhealthy", "fallos_consecutivos": 2, "exitos_consecutivos": 0}
}
```

## 6. Registro de eventos

Cada línea del archivo `ARCHIVO_LOG` es un objeto JSON con `timestamp` (UTC),
`componente: "ms-monitor"` y `evento`. Eventos:

| `evento` | Cuándo | Campos propios |
| --- | --- | --- |
| `inicio` | Al arrancar, tras inicializar Redis | `instancias`, `intervalo_ciclo_segundos` |
| `ciclo_iniciado` | Al comenzar cada ciclo | `numero_ciclo` |
| `resultado_probe` | Una vez por cada chequeo (6 por ciclo) | `instancia_id`, `numero_ciclo`, `tipo_probe` (`vida`\|`semantico`), `resultado` (`ok`\|`fallo`), `detalle`, `latencia_ms` |
| `cambio_estado` | Cuando una réplica cambia de estado | `instancia_id`, `numero_ciclo`, `estado_anterior`, `estado_nuevo`, `motivo` (`fallos_consecutivos`\|`exitos_consecutivos`) |
| `escritura_redis` | Tras cada `SET` exitoso en Redis | `instancia_id`, `numero_ciclo`, `estado_escrito` |
| `error_escritura_redis` | Si falla un `SET` en Redis | `instancia_id`, `numero_ciclo`, `detalle` |
| `error_ciclo` | Si un chequeo lanza una excepción inesperada | `numero_ciclo`, `detalle` |

El evento **`cambio_estado`** es el punto de referencia para medir cuánto tardó el
monitor en detectar una falla o en reintegrar una réplica recuperada.

## 7. Robustez

- Reintenta la conexión a Redis al arrancar (hasta 30 intentos, 1 s entre cada uno).
- Nunca se detiene por una excepción al llamar a una réplica: se trata como "fallo"
  del chequeo, no como error del monitor.
- Un fallo al escribir en Redis se registra y el ciclo continúa; el siguiente ciclo
  reintenta.

## 8. Ejecución

### Con Docker

```bash
docker build -t ms-monitor .
docker run --rm -p 6000:6000 \
  -e INSTANCIAS=instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000 \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  -e INTERVALO_CICLO_SEGUNDOS=5 -e TIMEOUT_PROBE_SEGUNDOS=3 \
  -e CICLOS_FALLIDOS_PARA_NO_SALUDABLE=2 -e CICLOS_EXITOSOS_PARA_REINTEGRAR=3 \
  -e ARCHIVO_LOG=/resultados/ms-monitor.jsonl -e PUERTO_DEBUG=6000 \
  -v "$(pwd)/resultados:/resultados" \
  ms-monitor
```

Necesita Redis y las tres réplicas de `ms-cotizacion` accesibles por red.

### En local (Python)

```bash
pip install -r requirements.txt
INSTANCIAS=instancia-1=http://localhost:5001,instancia-2=http://localhost:5002,instancia-3=http://localhost:5003 \
REDIS_HOST=localhost REDIS_PORT=6379 \
INTERVALO_CICLO_SEGUNDOS=5 TIMEOUT_PROBE_SEGUNDOS=3 \
CICLOS_FALLIDOS_PARA_NO_SALUDABLE=2 CICLOS_EXITOSOS_PARA_REINTEGRAR=3 \
ARCHIVO_LOG=./ms-monitor.jsonl PUERTO_DEBUG=6000 \
python app.py
```

## 9. Prueba rápida

```bash
curl http://localhost:6000/status         # las tres réplicas deben aparecer "healthy"

# Provocar una falla en una réplica y observar la detección
curl -X POST http://localhost:5003/inject-fault -H "Content-Type: application/json" \
  -d '{"tipo":"error_http"}'
sleep 18
curl http://localhost:6000/status         # instancia-3 debe pasar a "unhealthy"

curl -X POST http://localhost:5003/clear-fault
sleep 18
curl http://localhost:6000/status         # instancia-3 debe volver a "healthy"
```
