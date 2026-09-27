# Experimento 2 — Detección y reacción ante accesos indebidos a perfiles de riesgo

Sistema simulado de varios componentes que modela un flujo típico de consulta de perfiles de
riesgo crediticio, con detección asíncrona de patrones de acceso indebido y reacción automática
(bloqueo del actor responsable). El objetivo del experimento es medir qué tan bien un enfoque de
detección desacoplado del camino síncrono de las solicitudes —analizando el patrón de consumo
después de haber respondido, no antes— logra identificar accesos indebidos sin penalizar la
latencia percibida por quien consulta, y qué tan rápido y de forma confiable reacciona una vez
detectados.

Se estudian dos tipos de acceso indebido:

- **Violación de alcance**: un actor con rol `cliente` consulta el perfil de otro cliente distinto
  al suyo. Es detectable de forma determinística, sin necesidad de historial.
- **Patrón de volumen/diversidad sospechoso**: un actor con rol de alcance ampliado (`asesor` u
  `operaciones`, que legítimamente pueden consultar múltiples perfiles) consulta un volumen o una
  variedad de clientes anormalmente alta en poco tiempo. Es detectable solo de forma heurística,
  comparando contra umbrales calibrados.

## 1. Requisitos

- Docker y Docker Compose (plugin `docker compose`, no el binario `docker-compose` antiguo).
- Python 3.11 o 3.12 en el host, solo para ejecutar `harness-experimento` (no se empaqueta en
  Docker: actúa como cliente externo del sistema, igual que lo haría un actor real). En Windows,
  evitar Python 3.13/3.14: `pandas==2.2.2` (fijado en `harness-experimento/requirements.txt`) no
  publica wheels precompilados para esas versiones, por lo que `pip` intenta compilarlo desde el
  código fuente y falla si no hay Visual Studio Build Tools instalados. Ver
  [`harness-experimento/README.md`](harness-experimento/README.md#51-requisitos-previos) para el
  detalle de cómo preparar el entorno virtual.

## 2. Estructura del repositorio

```
Experimento_2/
├── docker-compose.yml          # Orquestación de los cinco servicios + Redis
├── .env                        # Umbrales de detección externalizados (ver sección 4)
├── api-gateway/                # incluye config/actores.json, el directorio estático de actores
├── servicio-perfilamiento/
├── monitor-accesos-indebidos/
├── orquestador-reaccion/
├── registro-auditoria/
└── harness-experimento/        # scripts CLI; no se levanta con docker compose
```

El directorio de actores (`config/actores.json`) vive dentro de `api-gateway/` porque es un dato de
configuración propio de ese componente —es el único que lo lee directamente—; `docker-compose.yml`
lo monta ahí como volumen de solo lectura para poder regenerarlo con
`harness-experimento/seed_actores.py` sin reconstruir ninguna imagen.

## 3. Puesta en marcha

### 3.1. Levantar el sistema

```bash
docker compose up --build -d
docker compose ps
```

Espera a que los cinco servicios con verificación de salud queden `healthy` (Redis, api-gateway,
servicio-perfilamiento, monitor-accesos-indebidos, orquestador-reaccion y registro-auditoria tienen
`healthcheck` configurado sobre `GET /salud`). Aun con `healthy`, dale unos segundos adicionales
antes de generar tráfico: que el proceso responda a `/salud` no garantiza todavía que
`monitor-accesos-indebidos` y `orquestador-reaccion` ya hayan completado su primera suscripción a
los canales de Redis (sus propios `/salud` sí lo confirman una vez suscritos, pero conviene
verificarlo explícitamente):

```bash
curl http://localhost:8000/salud
curl http://localhost:8002/salud
curl http://localhost:8003/salud
curl http://localhost:8004/salud
```

### 3.2. Sembrar datos de prueba

Dos siembras independientes, ambas idempotentes (se pueden repetir sin duplicar ni corromper
datos):

```bash
# Perfiles simulados en Redis (mínimo 50; servicio-perfilamiento los lee)
docker compose exec servicio-perfilamiento python seed_perfiles.py --cantidad 50

# Directorio de actores que lee api-gateway al arrancar (regenera api-gateway/config/actores.json)
cd harness-experimento
py -3.12 -m venv .venv          # una sola vez; ver nota sobre versión de Python en la sección 1
.venv\Scripts\Activate.ps1      # PowerShell; en bash/macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python seed_actores.py --num-clientes 10 --num-asesores 4 --num-operaciones 2
cd ..
```

`api-gateway` carga el directorio de actores **una sola vez al arrancar**, así que si se regenera
después de que el contenedor ya está corriendo hace falta reiniciarlo para que tome el nuevo
contenido:

```bash
docker compose up -d --no-deps api-gateway
```

### 3.3. Verificación de extremo a extremo

Antes de correr el experimento formalmente, conviene confirmar manualmente que el flujo completo
funciona:

```bash
# 1) Consulta legítima de un cliente sobre su propio perfil → 200
curl -i http://localhost:8000/perfil/cli-001 -H "X-Actor-Id: actor-cli-001"

# 2) Consulta indebida (alcance ajeno) → 200 igual (el Gateway no bloquea de antemano)
curl -i http://localhost:8000/perfil/cli-002 -H "X-Actor-Id: actor-cli-001"

# 3) Poco después debe aparecer el hallazgo correspondiente
curl "http://localhost:8004/eventos?tipo_registro=hallazgo"

# 4) Repetir la misma consulta indebida: el actor ya debería estar bloqueado → 403
curl -i http://localhost:8000/perfil/cli-002 -H "X-Actor-Id: actor-cli-001"
curl http://localhost:8003/bloqueados
```

Si los cuatro pasos se comportan como se describe, el sistema está listo para correr el
experimento.

## 4. Calibración de umbrales de detección

Los tres parámetros de la regla heurística de `monitor-accesos-indebidos` (`VENTANA_SEGUNDOS`,
`UMBRAL_VOLUMEN`, `UMBRAL_DIVERSIDAD`) se leen de variables de entorno y pueden cambiarse sin
reconstruir ninguna imagen, solo reiniciando ese contenedor. Los valores vigentes están en
[`.env`](.env):

```bash
# Editar el valor deseado en .env y luego:
docker compose up -d --no-deps monitor-accesos-indebidos

# o, para una corrida puntual sin tocar .env:
UMBRAL_DIVERSIDAD=12 docker compose up -d --no-deps monitor-accesos-indebidos
```

`monitor-accesos-indebidos/README.md` explica en detalle qué mide cada umbral y cómo usar
`GET /estado` para observar en vivo el efecto de un cambio mientras se genera tráfico.

## 5. Ejecutar el experimento

Con el sistema arriba, sembrado y verificado, el instrumento de medición es
[`harness-experimento`](harness-experimento/README.md), que corre desde el host (no dentro de
Docker) y actúa como cliente externo del sistema:

```bash
cd harness-experimento

# Combinación de prueba de humo: solo tráfico legítimo, sin ataque
python ejecutar_combinacion.py --combinacion 1 --repeticion 1 --salida resultados/comb1_rep1.jsonl

# Combinación con violación de alcance inyectada a mitad de la corrida
python ejecutar_combinacion.py --combinacion 2 --repeticion 1 --salida resultados/comb2_rep1.jsonl

# Combinación con ráfaga de diversidad + validación de deduplicación de hallazgos
python ejecutar_combinacion.py --combinacion 3 --repeticion 1 --salida resultados/comb3_rep1.jsonl

# Calcular métricas sobre todas las corridas generadas
python analizar_resultados.py resultados/comb*_rep*.jsonl
```

Esto produce `resultados/metricas_por_corrida.csv`, `resultados/metricas_por_combinacion.csv` y las
gráficas de tasa de falsos positivos y de latencia de detección en `resultados/graficas/`. Las
combinaciones de tráfico (mezcla de roles, duración, qué ataque inyectar y en qué instante) están
definidas en `harness-experimento/combinaciones.json` y se pueden ajustar o ampliar sin tocar
código. Para reproducir una corrida de calibración de umbrales, repetir este ciclo por cada valor
de umbral probado (sección 4), pasando `--umbral-bajo-prueba <valor>` a `ejecutar_combinacion.py`
para que quede asociado a esa corrida en las métricas.

Para las corridas formales, que requieren varias repeticiones por combinación,
`harness-experimento/ejecutar_lote_formal.py` automatiza el lanzamiento secuencial y puede
reanudarse si se interrumpe (ver `harness-experimento/README.md`, sección 5.4.1).

## 6. Operación del entorno

**Ver logs de un componente durante una corrida:**
```bash
docker compose logs -f monitor-accesos-indebidos
```

**Limpiar el estado de Redis entre bloques de corridas** (borra perfiles, ventanas, bloqueados y
claves de deduplicación; no toca el historial de `registro-auditoria`, que vive en SQLite):
```bash
docker compose exec redis redis-cli FLUSHDB
# recordar volver a ejecutar seed_perfiles.py después
```

**Apagar el entorno conservando los datos de auditoría:**
```bash
docker compose down
```

**Apagar el entorno y borrar también el histórico de auditoría (reinicio completo desde cero):**
```bash
docker compose down -v
```

## 7. Qué mide cada métrica del experimento

Definidas en detalle en `harness-experimento/README.md`; en resumen:

| Métrica | Qué responde |
| --- | --- |
| Tasa de detección | De los ataques inyectados, ¿qué proporción generó un hallazgo correlacionable en `registro-auditoria`? |
| Tasa de falsos positivos | Del tráfico legítimo, ¿qué proporción disparó un hallazgo indebidamente? |
| Latencia de detección (p95) | ¿Cuánto tiempo pasa entre que ocurre el acceso indebido y que `monitor-accesos-indebidos` lo detecta? |
| Latencia de reacción (p95) | ¿Cuánto tiempo pasa entre la detección y que `orquestador-reaccion` completa el bloqueo? |
| Tasa de reacciones idempotentes correctas | Ante una entrega duplicada del mismo hallazgo, ¿el sistema ejecutó el bloqueo una sola vez? |

Estas métricas, junto con las gráficas generadas, son el insumo directo para la sección de
resultados del informe del experimento.
