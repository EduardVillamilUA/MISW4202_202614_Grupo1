# ms-monitor — Monitor de Salud

**Responsable:** Fredy · **Experimento 1: Disponibilidad** (HA-DISP-13 / HA-DISP-14)

Implementa la táctica de **detección** del Cuaderno de Trabajo III: verificación
periódica y combinada (Ping/Echo + chequeo semántico) de las tres instancias de
`ms-cotizacion`, escribiendo el resultado en el Registro de Salud (Redis) para que
`ms-router` pueda enmascarar las fallas.

## Archivos

| Archivo | Descripción |
| --- | --- |
| `app.py` | Implementación completa del Monitor |
| `requirements.txt` | `flask`, `redis`, `requests` |
| `Dockerfile` | Imagen Python 3.11-slim (incluye `curl` para el healthcheck de Compose) |


## Cómo se integra

Copiar la carpeta `ms-monitor/` en la raíz del repositorio `experimento-1-disponibilidad/`
(estructura de la sección 1 de `05-docker-compose-y-despliegue.md`) y pegar el bloque del
fragmento dentro de `services:` en el `docker-compose.yml`.

Las variables compartidas (`INSTANCIAS`, `REDIS_HOST`, `REDIS_PORT`,
`INTERVALO_CICLO_SEGUNDOS`, `TIMEOUT_PROBE_SEGUNDOS`,
`CICLOS_FALLIDOS_PARA_NO_SALUDABLE`, `CICLOS_EXITOSOS_PARA_REINTEGRAR`) se leen del `.env`
de la raíz; solo `ARCHIVO_LOG` y `PUERTO_DEBUG` se declaran por servicio.

## Verificación local (sin Docker)

```bash
pip install flask redis requests fakeredis
python test_monitor_local.py
```

La prueba levanta tres instancias falsas de `ms-cotizacion`, sustituye Redis por
`fakeredis`, y valida los tres tipos de falla (`prima_inconsistente`, `error_http`,
`timeout`) más la reintegración, con el intervalo de ciclo reducido a 1 s para que la
prueba corra rápido.

## Verificación en el stack completo

```bash
mkdir -p resultados && docker-compose up -d
curl http://localhost:6000/status                     # las tres deben estar healthy

curl -X POST http://localhost:5003/inject-fault \
     -H "Content-Type: application/json" \
     -d '{"tipo":"prima_inconsistente"}'
sleep 15
curl http://localhost:6000/status                     # instancia-3 debe estar unhealthy

curl -X POST http://localhost:5003/clear-fault
sleep 20
curl http://localhost:6000/status                     # instancia-3 vuelve a healthy

grep cambio_estado resultados/ms-monitor.jsonl
```

## Eventos del log (`/resultados/ms-monitor.jsonl`)

`inicio` · `ciclo_iniciado` · `resultado_probe` · `cambio_estado` · `escritura_redis` ·
`error_escritura_redis` · `error_ciclo`

El evento **`cambio_estado`** es el que el script de análisis usa como instante de
detección y de reintegración: es el más importante de todo el experimento.
