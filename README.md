# MISW4202_202614_Grupo1

Repositorio Proyecto Arquitecturas Ágiles de Software - Grupo 1

Experimento 1 de Disponibilidad (HA-DISP-13 / HA-DISP-14) del caso Solventa. La
especificación completa está en los documentos `00` a `06` y `GUIA-EQUIPO-EJECUCION.md`
(no incluidos en este README); este archivo cubre solo cómo levantar, integrar y
ejecutar lo ya construido.

## Estructura del repositorio

```
experimento/
├── docker-compose.yml
├── .env                          # variables compartidas de ms-monitor y ms-router
├── ms-cotizacion/                # Sebastián — servicio de negocio (x3 instancias)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── ms-monitor/                   # Fredy — Monitor de Salud
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── ms-router/                    # Eduard — Router
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── harness/                      # Felipe — generador de tráfico, inyección de fallas y análisis
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── harness.py                 # ejecuta UNA corrida
│   ├── ejecutar_experimento.py    # orquesta las 60 corridas formales
│   └── analizar_resultados.py     # calcula métricas y veredicto
└── resultados/                   # volumen compartido; se llena en tiempo de ejecución
                                   # (los .jsonl no se versionan, ver .gitignore)
```

## Componentes y puertos

| Servicio | Puerto interno | Puerto publicado | Notas |
| --- | --- | --- | --- |
| `ms-cotizacion-1/2/3` | 5000 | 5001 / 5002 / 5003 | Misma imagen, cambia `INSTANCIA_ID` |
| `ms-monitor` | 6000 | 6000 | `GET /status` — solo depuración manual |
| `ms-router` | 8000 | 8000 | Punto de entrada del harness (`POST /cotizar`) |
| `redis` | 6379 | (no publicado) | Registro de Salud |
| `harness` | — | — | Perfil `herramientas`, no arranca con `docker compose up` |

## 1. Levantar el stack

```bash
mkdir -p resultados   # ya existe en el repo con un .gitkeep
docker compose up -d --build
docker compose ps     # confirmar que los 6 servicios quedan "healthy"
```

## 2. Prueba de integración manual (extremo a extremo)

Reproduce los 6 pasos de la sección 7 de `05-docker-compose-y-despliegue.md`. Debe
ejecutarla el equipo completo, en vivo, antes de correr las 60 corridas formales.

```bash
# 1-2. Ver arriba (docker compose up -d --build && docker compose ps)

# 3. Confirmar que el Monitor reporta las tres instancias "healthy"
curl http://localhost:6000/status

# 4. Cotización de prueba a través del Router (debe responder 200, prima 15000.0)
curl -X POST http://localhost:8000/cotizar \
  -H "Content-Type: application/json" \
  -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'

# 5. Prueba de humo de detección: inyectar error_http en instancia-3
curl -X POST http://localhost:5003/inject-fault \
  -H "Content-Type: application/json" -d '{"tipo":"error_http"}'
sleep 18
curl http://localhost:6000/status   # instancia-3 debe aparecer "unhealthy"
# enviar varias solicitudes al Router y confirmar que instancia_id nunca es instancia-3
for i in $(seq 1 10); do
  curl -s -X POST http://localhost:8000/cotizar \
    -H "Content-Type: application/json" \
    -d '{"cliente_id":"SINTETICO-CANARY","producto":"viaje","monto_asegurado":1000000}'
  echo
done

# 6. Prueba de humo de reintegración: limpiar la falla
curl -X POST http://localhost:5003/clear-fault
sleep 18
curl http://localhost:6000/status   # instancia-3 debe volver a "healthy"
# repetir las solicitudes del paso anterior y confirmar que instancia-3 vuelve al reparto
```

Después de esta prueba, limpia los `.jsonl` generados en `resultados/` antes de
iniciar las 60 corridas formales, para no mezclar tráfico sintético de prueba con
los datos oficiales:

```bash
rm -f resultados/*.jsonl
```

## 3. Ejecutar las 60 corridas formales

El servicio `harness` usa el perfil `herramientas`, por lo que **no** arranca con
`docker compose up`; se invoca explícitamente:

```bash
docker compose run --rm harness python ejecutar_experimento.py \
  --router-url http://ms-router:8000 \
  --monitor-status-url http://ms-monitor:6000/status \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --instancias instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000 \
  --directorio-resultados /resultados
```

Esto ejecuta las 60 corridas (20 por cada uno de `timeout`, `error_http`,
`prima_inconsistente`), reiniciando el estado del stack a "sano" entre cada una, y al
finalizar invoca automáticamente `analizar_resultados.py`, que deja en `resultados/`:

- `resultados_consolidados.csv`
- `resumen_por_tipo_falla.csv`
- `boxplot_tiempo_deteccion.png`, `boxplot_ventana_exposicion.png`, `boxplot_tiempo_reintegracion.png`
- el veredicto por tipo de falla impreso en consola

Duración aproximada: ~2 horas. Ver `06-protocolo-experimental-y-metricas.md` para el
detalle del protocolo, las métricas y los umbrales de aceptación.

### Ejecutar una sola corrida (depuración)

```bash
docker compose run --rm harness python harness.py \
  --tipo-falla error_http \
  --numero-corrida 1 \
  --router-url http://ms-router:8000 \
  --instancia-objetivo instancia-3 \
  --instancia-objetivo-url http://ms-cotizacion-3:5000 \
  --archivo-log /resultados/harness-corrida-error_http-1.jsonl
```

## 4. Apagar el stack

```bash
docker compose down
```

## Notas de integración

- `INSTANCIAS`, `REDIS_HOST`, `REDIS_PORT`, `INTERVALO_CICLO_SEGUNDOS`,
  `TIMEOUT_PROBE_SEGUNDOS`, `CICLOS_FALLIDOS_PARA_NO_SALUDABLE`,
  `CICLOS_EXITOSOS_PARA_REINTEGRAR` y `TIMEOUT_FORWARD_SEGUNDOS` viven en `.env` y
  son compartidas por `ms-monitor` y `ms-router` vía `env_file`.
- `ms-cotizacion/Dockerfile` y `ms-router/Dockerfile` instalan `curl` (igual que ya
  hacía `ms-monitor/Dockerfile`) para que Docker Compose pueda usar `healthcheck`
  basados en HTTP sobre esos servicios.
- El `healthcheck` de `ms-router` solo verifica que el puerto 8000 responde (no envía
  el payload real de `/cotizar`), para no inyectar tráfico sintético adicional durante
  las 60 corridas formales.
