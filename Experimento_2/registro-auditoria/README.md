# registro-auditoria

Recibe, desde `orquestador-reaccion`, cada hallazgo detectado y cada reacción de
seguridad ejecutada, y los persiste de forma **append-only** (solo inserción) en
SQLite. Expone además una consulta filtrable para que, al finalizar cada corrida del
experimento, se puedan extraer todos los registros y calcular sus métricas (latencia
de detección, latencia de reacción, manejo de duplicados, etc.).

**El punto de diseño central es que este componente nunca actualiza ni borra una fila
ya escrita.** No existe ningún endpoint de `PUT`, `PATCH` o `DELETE`. Es una
simplificación deliberada del principio de registro inmutable de auditoría: la
evidencia de lo ocurrido durante una corrida no debe poder alterarse después del
hecho. Si algo necesita "corregirse", la única forma es que llegue un nuevo evento —
nunca se modifica uno existente.

Este componente no decide nada por sí mismo: no interpreta si un hallazgo es correcto,
no valida que exista un registro de `hallazgo` antes de aceptar uno de `reaccion` para
el mismo `evento_id`, y no correlaciona eventos entre sí. Simplemente guarda, con
fidelidad, cada solicitud que recibe, tal como llega. La correlación y el análisis son
responsabilidad de quien procese los datos después de la corrida, no de este servicio
en el momento de escribir.

---

## 1. Cómo funciona

El componente mantiene una única tabla SQLite, `eventos_auditoria`, y expone tres
endpoints:

- `POST /eventos` inserta una fila nueva. Cada solicitud se guarda tal cual llega, sin
  intentar relacionarla con ninguna fila anterior. Por diseño del resto del sistema,
  `orquestador-reaccion` llama a este endpoint **dos veces por cada hallazgo que
  procesa**: una vez con `tipo_registro = "hallazgo"` (apenas lo recibe, antes de
  actuar) y otra con `tipo_registro = "reaccion"` (apenas termina de ejecutar el
  bloqueo). Esto es lo que permite, más adelante, medir por separado cuánto tiempo pasó
  entre que se detectó un patrón indebido y cuánto tiempo pasó hasta que se reaccionó
  ante él.
- `GET /eventos` devuelve todas las filas que cumplen los filtros opcionales
  recibidos, ordenadas por `id` ascendente. Es el punto de extracción de datos que usa
  el harness del experimento al final de cada corrida.
- `GET /salud` verifica que el archivo SQLite responde a una consulta trivial.

Cada fila queda marcada con `recibido_en`, una marca de tiempo generada por este mismo
componente en el instante de la escritura — distinta de `timestamp_evento_origen`,
`timestamp_deteccion` y `timestamp_reaccion`, que llegan ya calculados por quien generó
el evento. Esa distinción importa: `recibido_en` es lo único que refleja cuándo este
componente efectivamente persistió el dato, y es lo que se usa para filtrar por rango
de tiempo (`desde` / `hasta`) al aislar los registros de una corrida específica dentro
de un historial que puede acumular varias corridas.

### Por qué no hay validación de relaciones entre eventos

Sería posible, en `POST /eventos`, verificar que ya exista una fila de `hallazgo` con
el mismo `evento_id` antes de aceptar la de `reaccion` correspondiente. Deliberadamente
no se hace: ese tipo de validación agregaría acoplamiento entre este componente y la
lógica de negocio de quién lo llama, sin aportar nada a su responsabilidad real, que es
guardar con fidelidad lo que le llega. Si algún día llegara una fila "huérfana" (una
`reaccion` sin `hallazgo` previo, por ejemplo), eso es en sí mismo un dato interesante
para el análisis posterior, no un error que este componente deba impedir.

### Por qué una conexión SQLite nueva por solicitud

El servidor Flask corre con `threaded=True`, así que distintas solicitudes pueden
atenderse en hilos distintos al mismo tiempo. Una sola conexión SQLite compartida entre
hilos no es segura sin cuidados adicionales, así que cada solicitud abre su propia
conexión y la cierra al terminar. El costo de abrir una conexión es despreciable frente
al volumen de este experimento (unos pocos miles de filas por corrida), así que no hace
falta un pool de conexiones. La base de datos se abre en modo `WAL`
(*write-ahead logging*), que permite que una escritura (`POST /eventos`, frecuente
durante una corrida) y una lectura (`GET /eventos`, típicamente una consulta larga del
harness al final) convivan sin bloquearse mutuamente.

## 2. Tecnología

- Python 3.11 + Flask.
- `sqlite3` de la biblioteca estándar de Python — no se usa ningún ORM ni motor de
  base de datos adicional, porque el volumen y la complejidad de las consultas de este
  experimento no lo justifican.
- El archivo SQLite vive en un volumen persistente, para que los datos de una corrida
  sobrevivan a un reinicio del contenedor.

## 3. Esquema de datos

Tabla única `eventos_auditoria`, creada automáticamente al arrancar el proceso si no
existe:

| Columna | Tipo | Notas |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Identificador interno de la fila; no confundir con `evento_id` |
| `evento_id` | `TEXT` | UUID del evento de hallazgo |
| `tipo_registro` | `TEXT` | `hallazgo` o `reaccion` |
| `actor_id` | `TEXT` | |
| `client_id_consultado` | `TEXT` | |
| `tipo_deteccion` | `TEXT` | `deterministico` o `heuristico` |
| `razon` | `TEXT` | |
| `timestamp_evento_origen` | `TEXT` | ISO-8601, tal como llegó |
| `timestamp_deteccion` | `TEXT` | ISO-8601, tal como llegó |
| `timestamp_reaccion` | `TEXT` \| `NULL` | ISO-8601; nulo en filas de tipo `hallazgo` |
| `es_duplicado` | `INTEGER` | `0` o `1` (SQLite no tiene tipo booleano nativo) |
| `recibido_en` | `TEXT` | ISO-8601, generado por este componente al escribir la fila |

No se crean índices adicionales ni claves foráneas: el volumen de datos esperado no lo
requiere.

## 4. Configuración (variables de entorno)

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `RUTA_SQLITE_AUDITORIA` | `/app/data/auditoria.db` | Ruta del archivo SQLite dentro del volumen persistente |
| `PUERTO` | `8004` | Puerto en el que escucha Flask |

Si el directorio de `RUTA_SQLITE_AUDITORIA` no existe todavía, el proceso lo crea al
arrancar.

## 5. Endpoints

### `POST /eventos`

Inserta una fila nueva. Cuerpo de entrada esperado:

```json
{
  "evento_id": "9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
  "tipo_registro": "reaccion",
  "actor_id": "actor-ase-001",
  "client_id_consultado": "cli-045",
  "tipo_deteccion": "heuristico",
  "razon": "patron_diversidad",
  "timestamp_evento_origen": "2026-09-21T15:04:32.118Z",
  "timestamp_deteccion": "2026-09-21T15:04:32.401Z",
  "timestamp_reaccion": "2026-09-21T15:04:32.512Z",
  "es_duplicado": false
}
```

Campos obligatorios: `evento_id`, `tipo_registro`, `actor_id`, `client_id_consultado`,
`tipo_deteccion`, `razon`, `timestamp_evento_origen`, `timestamp_deteccion`,
`es_duplicado`. `timestamp_reaccion` es opcional (se guarda como `NULL` si no viene,
que es exactamente lo esperado en una fila de tipo `hallazgo`).

```bash
curl -X POST http://localhost:8004/eventos \
  -H "Content-Type: application/json" \
  -d '{
    "evento_id": "9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
    "tipo_registro": "hallazgo",
    "actor_id": "actor-ase-001",
    "client_id_consultado": "cli-045",
    "tipo_deteccion": "heuristico",
    "razon": "patron_diversidad",
    "timestamp_evento_origen": "2026-09-21T15:04:32.118Z",
    "timestamp_deteccion": "2026-09-21T15:04:32.401Z",
    "es_duplicado": false
  }'
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `201` | `{"id": <id_interno>}` | Fila insertada correctamente |
| `400` | `{"error": "campos_incompletos"}` | Falta algún campo obligatorio, o el cuerpo no es un JSON válido |
| `500` | `{"error": "fallo_persistencia"}` | La escritura en SQLite falló (por ejemplo, disco lleno o base de datos bloqueada) |

### `GET /eventos`

Devuelve un arreglo JSON con todas las filas que cumplen los filtros aplicados,
ordenadas por `id` ascendente. Todos los parámetros son opcionales y se pueden
combinar:

| Parámetro | Filtra por |
| --- | --- |
| `tipo_registro` | Igualdad exacta (`hallazgo` o `reaccion`) |
| `actor_id` | Igualdad exacta |
| `tipo_deteccion` | Igualdad exacta (`deterministico` o `heuristico`) |
| `desde` | `recibido_en >= desde` (ISO-8601) |
| `hasta` | `recibido_en <= hasta` (ISO-8601) |

```bash
curl "http://localhost:8004/eventos?tipo_registro=reaccion&actor_id=actor-ase-001"
```

```json
[
  {
    "id": 12,
    "evento_id": "9c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
    "tipo_registro": "reaccion",
    "actor_id": "actor-ase-001",
    "client_id_consultado": "cli-045",
    "tipo_deteccion": "heuristico",
    "razon": "patron_diversidad",
    "timestamp_evento_origen": "2026-09-21T15:04:32.118Z",
    "timestamp_deteccion": "2026-09-21T15:04:32.401Z",
    "timestamp_reaccion": "2026-09-21T15:04:32.512Z",
    "es_duplicado": false,
    "recibido_en": "2026-09-21T15:04:32.520Z"
  }
]
```

Sin ningún filtro, devuelve el historial completo. Este endpoint no pagina: para el
volumen de datos esperado en este experimento no hace falta.

### `GET /salud`

```bash
curl http://localhost:8004/salud
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | `{"estado": "ok"}` | El archivo SQLite respondió a una consulta trivial |
| `503` | `{"estado": "sqlite_no_disponible"}` | La base de datos no pudo consultarse |

## 6. Reinicio y aislamiento entre corridas

Este componente no borra datos automáticamente entre corridas: acumula todo el
historial en la misma tabla. Para aislar los resultados de una corrida en particular
hay dos opciones:

- **Filtrar por rango de tiempo** (`desde` / `hasta` en `GET /eventos`), conservando el
  historial completo de todas las corridas, incluidas las de calibración. Es el
  comportamiento recomendado por defecto, porque preserva evidencia completa.
- **Vaciar la tabla manualmente** (`DELETE FROM eventos_auditoria` ejecutado a mano
  contra el archivo SQLite) antes de un nuevo bloque de corridas, si se prefiere
  empezar cada bloque desde una base vacía. Esta operación es intencionalmente
  administrativa: no existe como endpoint HTTP, precisamente para que no pueda
  invocarse por accidente ni de forma remota.

## 7. Ejecución

### Con Docker

```bash
docker build -t registro-auditoria .
docker run --rm -p 8004:8004 \
  -v datos-auditoria:/app/data \
  -e RUTA_SQLITE_AUDITORIA=/app/data/auditoria.db \
  -e PUERTO=8004 \
  registro-auditoria
```

El volumen nombrado (`datos-auditoria` en el ejemplo) es lo que hace que el archivo
SQLite sobreviva a un reinicio del contenedor.

### En local (Python)

```bash
pip install -r requirements.txt
RUTA_SQLITE_AUDITORIA=./auditoria.db PUERTO=8004 python app.py
```

## 8. Casos de prueba verificados antes de integrar

1. `POST /eventos` con un cuerpo válido de tipo `hallazgo` → `201`, fila insertada con
   `timestamp_reaccion` nulo.
2. `POST /eventos` con un cuerpo válido de tipo `reaccion` para el mismo `evento_id` →
   `201`, una fila **adicional** (no una actualización de la anterior);
   `GET /eventos?tipo_registro=hallazgo` y `GET /eventos?tipo_registro=reaccion`
   devuelven conjuntos distintos.
3. `POST /eventos` con un campo obligatorio faltante → `400` con
   `{"error": "campos_incompletos"}`.
4. Reiniciar el contenedor (con el volumen persistente montado) y verificar que los
   datos siguen presentes en `GET /eventos`.
5. Insertar varias decenas de filas en distintos momentos y verificar que
   `GET /eventos` con `desde` y `hasta` devuelve exactamente el subconjunto esperado.

```bash
# Insertar un hallazgo y su reacción correspondiente, y verificarlos por separado
curl -X POST http://localhost:8004/eventos -H "Content-Type: application/json" -d '{
  "evento_id": "abc-123", "tipo_registro": "hallazgo", "actor_id": "actor-ase-001",
  "client_id_consultado": "cli-045", "tipo_deteccion": "heuristico",
  "razon": "patron_diversidad", "timestamp_evento_origen": "2026-09-21T15:04:32.118Z",
  "timestamp_deteccion": "2026-09-21T15:04:32.401Z", "es_duplicado": false
}'

curl -X POST http://localhost:8004/eventos -H "Content-Type: application/json" -d '{
  "evento_id": "abc-123", "tipo_registro": "reaccion", "actor_id": "actor-ase-001",
  "client_id_consultado": "cli-045", "tipo_deteccion": "heuristico",
  "razon": "patron_diversidad", "timestamp_evento_origen": "2026-09-21T15:04:32.118Z",
  "timestamp_deteccion": "2026-09-21T15:04:32.401Z",
  "timestamp_reaccion": "2026-09-21T15:04:32.512Z", "es_duplicado": false
}'

curl "http://localhost:8004/eventos?tipo_registro=hallazgo"
curl "http://localhost:8004/eventos?tipo_registro=reaccion"
```

## 9. Integración con el resto del experimento

Este componente no genera tráfico ni toma decisiones por sí mismo: es el destino final
de la evidencia que produce `orquestador-reaccion` cada vez que procesa un hallazgo.
Nadie más le escribe. Su único lector es el harness del experimento, que al final de
cada corrida consulta `GET /eventos` (típicamente filtrando por rango de tiempo) para
calcular las métricas de latencia de detección, latencia de reacción y manejo de
duplicados. Si este componente no está disponible, `orquestador-reaccion` sigue
ejecutando sus reacciones de seguridad con normalidad (ver la sección correspondiente
en su propio README): lo único que se pierde es la constancia escrita de lo ocurrido,
nunca la reacción en sí.
