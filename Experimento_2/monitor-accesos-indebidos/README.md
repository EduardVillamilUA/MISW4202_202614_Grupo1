# monitor-accesos-indebidos

Analiza en segundo plano, de forma continua, cada consulta de perfil que ocurre en el
sistema y decide si constituye un acceso indebido. Se suscribe a un canal de eventos de
acceso y, por cada uno, aplica dos reglas de detección independientes. Cuando alguna se
cumple, publica un evento de hallazgo en otro canal para que otro componente reaccione.

**No participa del camino síncrono de ninguna solicitud.** No responde consultas de
perfil ni decide bloqueos: solo observa lo que ya ocurrió y notifica. Los endpoints HTTP
que expone son exclusivamente de diagnóstico (verificar que el proceso está sano y
consultar el estado vigente de un actor), no forman parte de la detección en sí.

---

## 1. Cómo funciona

Un hilo de fondo, independiente del ciclo de peticiones HTTP de Flask, se mantiene
suscrito al canal `canal:acceso-perfil` mientras el proceso esté vivo. Por cada evento
de acceso recibido, se evalúan en orden dos reglas:

### Regla 1 — Determinística (alcance)

Aplica solo a actores con `rol_actor == "cliente"`. Un cliente tiene un único
`client_id` propio que puede consultar legítimamente; si el `client_id_consultado` del
evento es distinto de su `alcance_propio`, es un acceso indebido de inmediato, sin
necesidad de historial ni de ningún estado acumulado:

- `tipo_deteccion`: `deterministico`
- `razon`: `violacion_alcance`

Si esta regla aplica (el actor es `cliente`), **no se evalúa la regla heurística** para
ese evento: no tiene sentido, porque esa regla está pensada para roles que no tienen un
único `client_id` propio contra el cual comparar.

### Regla 2 — Heurística (volumen y diversidad)

Aplica a actores cuyo rol está en la lista de roles de alcance ampliado (por defecto
`asesor` y `operaciones`, configurable). Estos roles no tienen un `client_id` propio
fijo, así que la única señal disponible es el *patrón* de consumo: cuántas consultas
hace un mismo actor y a cuántos clientes distintos, dentro de una ventana de tiempo
deslizante.

Por cada evento de un actor con rol de alcance ampliado:

1. Se registra la consulta en una ventana deslizante propia de ese actor, guardada como
   un *sorted set* de Redis (`ventana:{actor_id}`), donde el score es el timestamp en
   milisegundos y el miembro combina timestamp y `client_id` consultado.
2. Se purgan de la ventana las entradas más antiguas que `VENTANA_SEGUNDOS`.
3. Se cuentan las consultas vigentes (`consultas_totales`) y los clientes distintos
   vigentes (`clientes_distintos`) dentro de la ventana.
4. Si `consultas_totales > UMBRAL_VOLUMEN` o `clientes_distintos > UMBRAL_DIVERSIDAD`,
   la consulta que acaba de llegar (no las anteriores) se marca como indebida:
   - `tipo_deteccion`: `heuristico`
   - `razon`: `patron_volumen`, `patron_diversidad`, o `patron_volumen_y_diversidad` si
     se superan ambos umbrales a la vez.

### Publicación del hallazgo

Cuando cualquiera de las dos reglas marca una consulta como indebida, se publica un
evento en `canal:hallazgos`. El `timestamp_deteccion` se captura justo antes de
publicar (no antes), para que la diferencia contra el momento en que ocurrió la
consulta original refleje la latencia real de detección.

### Por qué la ventana vive en Redis y no en memoria del proceso

El estado de cada actor sobrevive a un reinicio del contenedor porque no depende de
variables en memoria de este proceso. Esto también deja preparado el componente para el
caso (fuera del alcance actual, donde solo corre una réplica) de que llegara a correr
más de una instancia: todas verían la misma ventana por actor, sin duplicar ni perder
conteo.

### Sobre la naturaleza del canal de eventos

La suscripción a `canal:acceso-perfil` es una suscripción Pub/Sub estándar de Redis:
solo se reciben los mensajes publicados mientras hay un suscriptor activo. Si este
componente está caído en el momento en que se publica un evento de acceso, ese evento
se pierde y no hay forma de recuperarlo después. Es una limitación conocida y aceptada
del diseño; la mitigación es de orden operativo (asegurar que este componente esté
arriba y suscrito antes de generar tráfico), no un problema que deba resolverse
cambiando la tecnología de mensajería.

Si la conexión con Redis se cae mientras el hilo está suscrito, el hilo reintenta la
suscripción tras una espera corta en lugar de terminar: una caída transitoria de Redis
no debe tumbar la capacidad de detección del resto de la corrida.

## 2. Tecnología

- Python 3.11 + Flask, usado únicamente para los endpoints de diagnóstico (`/salud`,
  `/estado`). La lógica de detección corre en un hilo de fondo (`threading.Thread`,
  daemon) separado del ciclo de peticiones HTTP, iniciado en cuanto se importa el
  módulo, no dentro del bloque de arranque del servidor Flask.
- `redis` (cliente `redis-py`): `pubsub()` para consumir `canal:acceso-perfil`,
  `publish()` para publicar en `canal:hallazgos`, y `ZADD` / `ZREMRANGEBYSCORE` /
  `ZRANGE` para mantener la ventana deslizante por actor.
- Sin base de datos propia ni llamadas HTTP salientes a otros servicios: toda la
  comunicación con el resto del sistema pasa por Redis.

## 3. Configuración (variables de entorno)

| Variable | Valor por defecto | Descripción |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | Host de Redis |
| `REDIS_PORT` | `6379` | Puerto de Redis |
| `VENTANA_SEGUNDOS` | `30` | Tamaño de la ventana deslizante por actor |
| `UMBRAL_VOLUMEN` | `8` | Umbral de consultas totales vigentes en la ventana |
| `UMBRAL_DIVERSIDAD` | `8` | Umbral de clientes distintos vigentes en la ventana (valor calibrado empíricamente por el equipo antes de las corridas formales) |
| `ROLES_ALCANCE_AMPLIADO` | `asesor,operaciones` | Lista de roles, separados por coma, a los que se les aplica la regla heurística |
| `PUERTO` | `8002` | Puerto en el que escucha Flask dentro del contenedor |

Los tres umbrales (`VENTANA_SEGUNDOS`, `UMBRAL_VOLUMEN`, `UMBRAL_DIVERSIDAD`) se leen
**una sola vez al arrancar el proceso**. Para cambiar de umbral hay que reiniciar el
contenedor con un nuevo valor de entorno; no existe un mecanismo de recarga en caliente,
a propósito: no aporta valor para el uso previsto de este componente y agregaría
complejidad innecesaria.

Los nombres de los canales (`canal:acceso-perfil`, `canal:hallazgos`) y el tiempo de
espera entre reintentos de reconexión están fijados en el código: son detalles internos,
no valores que otro componente necesite configurar.

## 4. Endpoints

### `GET /estado?actor_id=<actor_id>`

Devuelve el estado vigente de la ventana deslizante de un actor y los umbrales activos
en este momento. Antes de leer, purga las entradas vencidas de la ventana, así que el
conteo devuelto siempre refleja el estado real al instante de la consulta, no un valor
desactualizado a la espera de que llegue una nueva consulta del actor que dispare la
purga.

Es la herramienta principal para calibrar los umbrales: permite observar en vivo cómo
evoluciona el conteo de un actor mientras se genera tráfico, sin depender de que ya se
haya publicado ningún hallazgo.

```bash
curl "http://localhost:8002/estado?actor_id=actor-ase-001"
```

```json
{
  "actor_id": "actor-ase-001",
  "ventana_segundos": 30,
  "umbral_volumen": 8,
  "umbral_diversidad": 5,
  "consultas_totales_vigentes": 6,
  "clientes_distintos_vigentes": 4
}
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | Estado de la ventana (ver arriba) | Consulta resuelta |
| `400` | `{"error": "actor_id_requerido"}` | Falta el parámetro `actor_id` |
| `503` | `{"error": "estado_no_disponible"}` | Redis no respondió a la consulta |

### `GET /salud`

```bash
curl http://localhost:8002/salud
```

| Código | Cuerpo | Situación |
| --- | --- | --- |
| `200` | `{"estado": "ok", "segundos_desde_ultimo_evento": 4.31}` | Redis responde y el hilo de consumo está activo. El campo `segundos_desde_ultimo_evento` es informativo y solo aparece una vez que se procesó al menos un evento desde que arrancó el proceso; no afecta el código de respuesta. |
| `503` | `{"estado": "redis_no_disponible"}` | Redis no responde a `PING` |
| `503` | `{"estado": "hilo_consumidor_caido"}` | El hilo de fondo que consume el canal terminó de forma inesperada |

## 5. Ejecución

### Con Docker

```bash
docker build -t monitor-accesos-indebidos .
docker run --rm -p 8002:8002 \
  -e REDIS_HOST=redis -e REDIS_PORT=6379 \
  -e VENTANA_SEGUNDOS=30 -e UMBRAL_VOLUMEN=8 -e UMBRAL_DIVERSIDAD=8 \
  -e ROLES_ALCANCE_AMPLIADO=asesor,operaciones \
  -e PUERTO=8002 \
  monitor-accesos-indebidos
```

Necesita Redis accesible por red (por nombre de servicio en una red de Docker Compose,
o por `localhost` con el puerto publicado en pruebas locales). Para que no se pierda
tráfico, este componente debe estar arriba y suscrito **antes** de que se empiece a
generar tráfico de consultas de perfil.

### En local (Python)

```bash
pip install -r requirements.txt
REDIS_HOST=localhost REDIS_PORT=6379 PUERTO=8002 python app.py
```

## 6. Casos de prueba verificados antes de integrar

1. Evento con `rol_actor = "cliente"` y `client_id_consultado != alcance_propio` →
   se publica un hallazgo `deterministico` / `violacion_alcance` de inmediato.
2. Evento con `rol_actor = "cliente"` y `client_id_consultado == alcance_propio` → no se
   publica ningún hallazgo.
3. Actor con `rol_actor = "asesor"` consultando pocos perfiles espaciados en el tiempo
   (por debajo de ambos umbrales) → no se publica ningún hallazgo, y `GET /estado`
   refleja el conteo correcto en cada momento.
4. El mismo actor superando `UMBRAL_DIVERSIDAD` dentro de la ventana → se publica un
   hallazgo `heuristico` / `patron_diversidad` justo en la consulta que hace superar el
   umbral, no antes ni después.
5. Esperar a que pase `VENTANA_SEGUNDOS` sin nuevas consultas del mismo actor y
   verificar, vía `GET /estado`, que el conteo vuelve a cero.
6. Detener este componente, generar tráfico (que se pierde, por no haber un suscriptor
   activo en ese momento), volver a levantarlo y confirmar que el sistema sigue
   funcionando con normalidad para el tráfico nuevo, sin quedar en un estado roto por
   los eventos perdidos mientras estuvo caído.

```bash
redis-cli SUBSCRIBE canal:hallazgos
# en otra terminal, generar una consulta fuera de alcance a través del gateway,
# o publicar manualmente un evento de prueba en canal:acceso-perfil
```

## 7. Integración con el resto del experimento

Este componente no hace nada por sí solo: necesita que `servicio-perfilamiento`
publique eventos en `canal:acceso-perfil` para tener algo que analizar, y que otro
componente esté suscrito a `canal:hallazgos` para darle uso a los hallazgos que publica
(actuar sobre ellos, por ejemplo bloqueando al actor responsable). Tampoco aplica ningún
bloqueo ni modifica el comportamiento del resto del sistema: su única salida es el
evento de hallazgo; la decisión de qué hacer con esa información es responsabilidad de
quien se suscribe a `canal:hallazgos`.
