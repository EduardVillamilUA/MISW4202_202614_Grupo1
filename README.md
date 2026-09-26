# MISW4202_202614_Grupo1

Repositorio Proyecto Arquitecturas Ágiles de Software — Grupo 1.

Este repositorio contiene **dos experimentos independientes**, cada uno en su propia carpeta,
con su propio `docker-compose.yml`, su propio código y su propio README con las instrucciones
completas de ejecución e interpretación de resultados.

## Experimentos

| Carpeta | Experimento | Qué valida |
| --- | --- | --- |
| [`Experimento_1/`](Experimento_1/README.md) | **Disponibilidad** (HA-DISP-13 / HA-DISP-14) | Detección de fallas (`ms-monitor`) y enmascaramiento/reintegración transparente de instancias no saludables (`ms-router`) en el servicio de Cotización. |
| [`Experimento_2/`](Experimento_2/README.md) | **Detección y reacción ante accesos indebidos** | Detección asíncrona de accesos indebidos a perfiles de riesgo crediticio (violación de alcance y patrones de volumen/diversidad sospechosos) y reacción automática (bloqueo del actor). |

Cada carpeta es autocontenida: tiene su propio `docker-compose.yml`, variables de entorno y
harness de pruebas, y no depende de archivos fuera de su propia carpeta. Para ejecutar o revisar
cualquiera de los dos, entra a la carpeta correspondiente y sigue las instrucciones de su README.

## Cómo empezar

1. Elige el experimento que quieres revisar o ejecutar.
2. Entra a su carpeta (`cd Experimento_1` o `cd Experimento_2`).
3. Sigue el README de esa carpeta desde su sección de requisitos: allí está el detalle completo
   de cómo levantar los servicios, correr el harness y leer los resultados.
