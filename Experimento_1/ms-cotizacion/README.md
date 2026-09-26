# ms-cotizacion

Servicio de negocio que calcula la **prima** de una cotización de seguro. Es el
servicio que se pone a prueba durante el experimento de disponibilidad: se despliega
**tres veces** con la misma imagen (cambiando solo su identificador) y cada réplica
puede **simular fallas controladas** y revertirlas sin reiniciarse.

---

## 1. Qué hace

- Expone un endpoint de cotización (`POST /cotizar`) y un endpoint de vida
  (`GET /health`).
- Calcula la prima con una fórmula fija y determinística (sin base de datos, sin
  llamadas a otros servicios).
- Puede activar, bajo petición, una de tres fallas simuladas (`timeout`,
  `error_http`, `prima_inconsistente`) y limpiarlas después.

## 2. Tecnología

- Python 3.11 + Flask (servidor de desarrollo con `threaded=True`).
- Sin persistencia: el estado de falla vive solo en memoria del proceso y se pierde
  al reiniciar.

## 3. Configuración (variables de entorno)

| Variable | Obligatoria | Ejemplo | Descripción |
| --- | --- | --- | --- |
| `INSTANCIA_ID` | Sí | `instancia-1` | Identificador de la réplica; aparece en todas las respuestas y logs |
| `PUERTO` | Sí | `5000` | Puerto en el que Flask escucha dentro del contenedor |
| `ARCHIVO_LOG` | Sí | `/resultados/ms-cotizacion-instancia-1.jsonl` | Ruta del archivo de eventos (JSON Lines) |

## 4. Endpoints

### `GET /health` — chequeo de vida

Respuesta normal (`200`):

```json
{"status": "ok", "instancia_id": "instancia-1"}
```

### `POST /cotizar` — cálculo de la prima

Solicitud:

```json
{"cliente_id": "SINTETICO-CANARY", "producto": "viaje", "monto_asegurado": 1000000}
```

Respuesta exitosa (`200`):

```json
{
  "cliente_id": "SINTETICO-CANARY",
  "producto": "viaje",
  "monto_asegurado": 1000000,
  "prima": 15000.0,
  "instancia_id": "instancia-1",
  "timestamp": "2025-01-01T12:00:03.245123Z"
}
```

**Fórmula:** `prima = monto_asegurado * tasa[producto]`

| `producto` | Tasa |
| --- | --- |
| `viaje` | 0.015 |
| `dispositivo` | 0.03 |
| `vida` | 0.008 |

Si `producto` no está en la tabla, responde `400 {"error": "producto no soportado"}`.
El campo `instancia_id` siempre indica qué réplica atendió la solicitud.

### `POST /inject-fault` — activar una falla simulada

```json
{"tipo": "timeout"}
```

Valores válidos de `tipo`: `"timeout"`, `"error_http"`, `"prima_inconsistente"`.
Un valor no válido devuelve `400 {"error": "tipo de falla no soportado"}` y no
cambia el estado. Respuesta correcta:

```json
{"status": "fault_injected", "tipo": "timeout", "instancia_id": "instancia-1"}
```

### `POST /clear-fault` — desactivar la falla (sin cuerpo)

```json
{"status": "fault_cleared", "instancia_id": "instancia-1"}
```

> `POST /inject-fault` y `POST /clear-fault` existen **solo para ejercitar el
> servicio en pruebas de disponibilidad**. No deben exponerse en un despliegue real.

## 5. Comportamiento según la falla activa

| Falla activa | `GET /health` | `POST /cotizar` |
| --- | --- | --- |
| ninguna | `200 {"status":"ok",...}` | `200` con la prima correcta |
| `timeout` | Duerme 10 s antes de responder (el cliente normalmente cancela antes por su propio timeout) | Igual: duerme 10 s |
| `error_http` | `500 {"status":"error",...}` | `500 {"error":"fallo interno simulado",...}` |
| `prima_inconsistente` | Responde con normalidad (`200 ok`) | `200` con la prima **inflada un 50 %** (`prima_correcta * 1.5`) |

`prima_inconsistente` es deliberadamente indetectable por un simple chequeo de vida:
el servicio parece sano (`GET /health` responde `200`), pero el resultado de negocio
es incorrecto.

## 6. Registro de eventos

Cada línea del archivo `ARCHIVO_LOG` es un objeto JSON con `timestamp` (UTC),
`componente: "ms-cotizacion"` y `evento`. Eventos:

| `evento` | Cuándo | Campos propios |
| --- | --- | --- |
| `inicio` | Al arrancar | `instancia_id` |
| `falla_inyectada` | Al recibir `POST /inject-fault` válido | `instancia_id`, `tipo` |
| `falla_limpiada` | Al recibir `POST /clear-fault` | `instancia_id` |
| `health_check_recibido` | En cada `GET /health` | `instancia_id`, `resultado` (`ok`\|`error`\|`timeout`) |
| `cotizacion_recibida` | En cada `POST /cotizar` | `instancia_id`, `cliente_id`, `producto`, `monto_asegurado`, `prima_calculada` (o `null`), `es_sintetica`, `resultado` |

## 7. Ejecución

### Con Docker

```bash
docker build -t ms-cotizacion .
docker run --rm -p 5001:5000 \
  -e INSTANCIA_ID=instancia-1 \
  -e PUERTO=5000 \
  -e ARCHIVO_LOG=/resultados/ms-cotizacion-instancia-1.jsonl \
  -v "$(pwd)/resultados:/resultados" \
  ms-cotizacion
```

### En local (Python)

```bash
pip install -r requirements.txt
INSTANCIA_ID=instancia-1 PUERTO=5000 ARCHIVO_LOG=./ms-cotizacion-instancia-1.jsonl python app.py
```

## 8. Prueba rápida

```bash
curl http://localhost:5001/health

curl -X POST http://localhost:5001/cotizar -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'
# prima esperada: 15000.0

curl -X POST http://localhost:5001/inject-fault -H "Content-Type: application/json" \
  -d '{"tipo":"prima_inconsistente"}'
curl -X POST http://localhost:5001/cotizar -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'
# prima ahora: 22500.0 (15000 * 1.5)

curl -X POST http://localhost:5001/clear-fault
```
