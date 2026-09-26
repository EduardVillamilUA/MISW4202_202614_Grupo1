"""
comun.py
========
Utilidades compartidas por los scripts del harness: carga del directorio de
actores, formato de timestamps, y un cliente HTTP delgado hacia api-gateway
que registra cada solicitud enviada en un archivo JSON Lines compartido.

No es un script ejecutable por sí mismo: todos los demás scripts de este
componente lo importan. Se mantiene deliberadamente pequeño y sin lógica de
negocio propia (esa vive en cada script), para que sea fácil verificar que
no introduce ningún comportamiento oculto compartido entre ellos.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

import requests


def formatear_timestamp(momento: datetime) -> str:
    """Serializa un datetime UTC como ISO-8601 con milisegundos y sufijo 'Z'."""
    return momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"


def cargar_directorio_actores(ruta: str) -> dict:
    """
    Lee el directorio de actores desde el mismo archivo JSON que usa
    api-gateway para resolver identidades. El harness lo necesita para saber
    qué actores existen, cuál es su rol y, si aplica, cuál es su
    client_id_propio, y así poder generar tráfico que un actor real del
    sistema podría generar.
    """
    with open(ruta, "r", encoding="utf-8") as f:
        directorio = json.load(f)
    if not isinstance(directorio, dict) or not directorio:
        raise ValueError(f"El directorio de actores en '{ruta}' está vacío o mal formado")
    return directorio


def actores_por_rol(directorio: dict, rol: str) -> list:
    return [actor_id for actor_id, datos in directorio.items() if datos.get("rol") == rol]


class RegistradorSolicitudes:
    """
    Archivo JSON Lines donde cada script de tráfico anota, de forma
    incremental y segura entre hilos, cada solicitud que envía.

    Se abre en modo 'a' y se hace flush después de cada línea: si el proceso
    se interrumpe a la mitad de una corrida (por ejemplo con Ctrl+C durante
    una prueba manual), las solicitudes ya enviadas quedan en disco en vez
    de perderse en un buffer sin escribir. El lock evita que dos hilos
    escriban al mismo tiempo y entrelacen sus líneas.
    """

    def __init__(self, ruta: str):
        directorio_padre = os.path.dirname(os.path.abspath(ruta))
        os.makedirs(directorio_padre, exist_ok=True)
        self._ruta = ruta
        self._lock = threading.Lock()

    def registrar(self, registro: dict) -> None:
        linea = json.dumps(registro, ensure_ascii=False)
        with self._lock:
            with open(self._ruta, "a", encoding="utf-8") as f:
                f.write(linea + "\n")


def consultar_perfil(url_gateway: str, actor_id: str, client_id: str, timeout_segundos: float = 5) -> dict:
    """
    Envía una solicitud GET /perfil/<client_id> a api-gateway en nombre de
    actor_id, tal como lo haría un cliente real del sistema, y devuelve un
    registro con los tiempos y el resultado, listo para pasarle a
    RegistradorSolicitudes.registrar() (el llamador todavía debe agregarle
    los campos es_trafico_legitimo / es_ataque_inyectado / patron_ataque,
    porque esa clasificación depende de quién está generando la solicitud,
    no de la solicitud en sí).

    Un error de red (timeout, conexión rechazada) también se registra, con
    codigo_http=None, en vez de propagar la excepción: una falla de red es
    en sí misma un resultado observable de la solicitud, no un motivo para
    abortar la generación de tráfico.
    """
    timestamp_envio = formatear_timestamp(datetime.now(timezone.utc))
    try:
        respuesta = requests.get(
            f"{url_gateway}/perfil/{client_id}",
            headers={"X-Actor-Id": actor_id},
            timeout=timeout_segundos,
        )
        codigo_http = respuesta.status_code
    except requests.exceptions.RequestException as exc:
        codigo_http = None
        logging.getLogger("harness").error(
            "fallo_solicitud actor_id=%s client_id=%s error=%s", actor_id, client_id, exc
        )
    timestamp_respuesta = formatear_timestamp(datetime.now(timezone.utc))

    return {
        "actor_id": actor_id,
        "client_id_consultado": client_id,
        "timestamp_envio": timestamp_envio,
        "timestamp_respuesta": timestamp_respuesta,
        "codigo_http": codigo_http,
    }
