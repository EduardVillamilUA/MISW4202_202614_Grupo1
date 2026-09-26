"""
registro-auditoria
====================
Recibe, desde orquestador-reaccion, cada hallazgo y cada reacción ejecutada,
y los persiste de forma append-only (solo inserción) en SQLite. Expone
también una consulta filtrable para que el harness del experimento pueda
extraer todos los registros de una corrida y calcular sus métricas.

Este componente es deliberadamente solo-inserción: no existe ningún endpoint
de actualización ni de borrado. Es una simplificación del principio de
registro inmutable de auditoría — la evidencia de una corrida no debe poder
alterarse después de escrita, y la única forma de "corregir" algo es dejar
constancia de un nuevo evento, nunca modificar uno ya guardado.

No valida relaciones entre hallazgo y reacción (por ejemplo, que exista un
registro de tipo "hallazgo" antes de aceptar uno de tipo "reaccion" con el
mismo evento_id): cada solicitud se persiste de forma independiente, tal
como llega. Esa correlación es responsabilidad de quien analiza los datos
después de la corrida, no de este componente en el momento de escribir.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

RUTA_SQLITE_AUDITORIA = os.environ.get("RUTA_SQLITE_AUDITORIA", "/app/data/auditoria.db")
PUERTO = int(os.environ.get("PUERTO", "8004"))

CAMPOS_OBLIGATORIOS = (
    "evento_id",
    "tipo_registro",
    "actor_id",
    "client_id_consultado",
    "tipo_deteccion",
    "razon",
    "timestamp_evento_origen",
    "timestamp_deteccion",
    "es_duplicado",
)

# Parámetros de consulta que GET /eventos acepta para filtrar; cada uno se
# traduce en una condición "columna = ?" salvo desde/hasta, que comparan por
# rango contra recibido_en.
FILTROS_IGUALDAD = ("tipo_registro", "actor_id", "tipo_deteccion")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s registro-auditoria %(levelname)s %(message)s",
)
logger = logging.getLogger("registro-auditoria")


def _formatear_timestamp(momento: datetime) -> str:
    """Serializa un datetime UTC como ISO-8601 con milisegundos y sufijo 'Z'."""
    return momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Acceso a SQLite
# ---------------------------------------------------------------------------
#
# Se abre una conexión nueva por solicitud (en vez de compartir una única
# conexión entre hilos) porque sqlite3 no garantiza que una misma conexión
# sea segura para usarse desde varios hilos a la vez, y el servidor Flask
# corre con threaded=True. El costo de abrir una conexión es despreciable
# frente al volumen de este experimento (unos pocos miles de filas por
# corrida), así que no hace falta un pool.

os.makedirs(os.path.dirname(RUTA_SQLITE_AUDITORIA), exist_ok=True)


def _crear_tabla_si_falta(conexion: sqlite3.Connection) -> None:
    conexion.execute(
        """
        CREATE TABLE IF NOT EXISTS eventos_auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id TEXT NOT NULL,
            tipo_registro TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            client_id_consultado TEXT NOT NULL,
            tipo_deteccion TEXT NOT NULL,
            razon TEXT NOT NULL,
            timestamp_evento_origen TEXT NOT NULL,
            timestamp_deteccion TEXT NOT NULL,
            timestamp_reaccion TEXT,
            es_duplicado INTEGER NOT NULL,
            recibido_en TEXT NOT NULL
        )
        """
    )
    conexion.commit()


def _conectar() -> sqlite3.Connection:
    conexion = sqlite3.connect(RUTA_SQLITE_AUDITORIA, timeout=5)
    conexion.row_factory = sqlite3.Row
    return conexion


# El modo WAL permite que una escritura y varias lecturas convivan sin
# bloquearse mutuamente, lo que importa aquí porque GET /eventos (lecturas
# potencialmente largas, del harness) y POST /eventos (escrituras frecuentes,
# del orquestador) ocurren en paralelo durante una corrida.
_conexion_inicial = _conectar()
_conexion_inicial.execute("PRAGMA journal_mode=WAL")
_crear_tabla_si_falta(_conexion_inicial)
_conexion_inicial.close()


def _obtener_conexion() -> sqlite3.Connection:
    """Devuelve la conexión SQLite de la solicitud actual, abriéndola si hace falta."""
    if "conexion_sqlite" not in g:
        g.conexion_sqlite = _conectar()
    return g.conexion_sqlite


# ---------------------------------------------------------------------------
# Aplicación Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.teardown_appcontext
def _cerrar_conexion(excepcion=None):
    conexion = g.pop("conexion_sqlite", None)
    if conexion is not None:
        conexion.close()


@app.route("/eventos", methods=["POST"])
def crear_evento():
    cuerpo = request.get_json(silent=True)
    if not isinstance(cuerpo, dict):
        return jsonify({"error": "campos_incompletos"}), 400

    faltantes = [campo for campo in CAMPOS_OBLIGATORIOS if campo not in cuerpo]
    if faltantes:
        logger.warning("evento_incompleto faltantes=%s cuerpo=%s", faltantes, cuerpo)
        return jsonify({"error": "campos_incompletos"}), 400

    conexion = _obtener_conexion()
    try:
        cursor = conexion.execute(
            """
            INSERT INTO eventos_auditoria (
                evento_id, tipo_registro, actor_id, client_id_consultado,
                tipo_deteccion, razon, timestamp_evento_origen, timestamp_deteccion,
                timestamp_reaccion, es_duplicado, recibido_en
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cuerpo["evento_id"],
                cuerpo["tipo_registro"],
                cuerpo["actor_id"],
                cuerpo["client_id_consultado"],
                cuerpo["tipo_deteccion"],
                cuerpo["razon"],
                cuerpo["timestamp_evento_origen"],
                cuerpo["timestamp_deteccion"],
                cuerpo.get("timestamp_reaccion"),
                1 if cuerpo["es_duplicado"] else 0,
                _formatear_timestamp(datetime.now(timezone.utc)),
            ),
        )
        conexion.commit()
    except sqlite3.Error as exc:
        logger.error("fallo_persistencia evento_id=%s error=%s", cuerpo.get("evento_id"), exc)
        return jsonify({"error": "fallo_persistencia"}), 500

    return jsonify({"id": cursor.lastrowid}), 201


@app.route("/eventos", methods=["GET"])
def listar_eventos():
    condiciones = []
    parametros = []

    for nombre_filtro in FILTROS_IGUALDAD:
        valor = request.args.get(nombre_filtro)
        if valor is not None:
            condiciones.append(f"{nombre_filtro} = ?")
            parametros.append(valor)

    desde = request.args.get("desde")
    if desde is not None:
        condiciones.append("recibido_en >= ?")
        parametros.append(desde)

    hasta = request.args.get("hasta")
    if hasta is not None:
        condiciones.append("recibido_en <= ?")
        parametros.append(hasta)

    consulta = "SELECT * FROM eventos_auditoria"
    if condiciones:
        consulta += " WHERE " + " AND ".join(condiciones)
    consulta += " ORDER BY id ASC"

    conexion = _obtener_conexion()
    filas = conexion.execute(consulta, parametros).fetchall()

    eventos = []
    for fila in filas:
        evento = dict(fila)
        evento["es_duplicado"] = bool(evento["es_duplicado"])
        eventos.append(evento)

    return jsonify(eventos), 200


@app.route("/salud", methods=["GET"])
def salud():
    try:
        _obtener_conexion().execute("SELECT 1")
    except sqlite3.Error as exc:
        logger.error("sqlite_no_disponible error=%s", exc)
        return jsonify({"estado": "sqlite_no_disponible"}), 503
    return jsonify({"estado": "ok"}), 200


if __name__ == "__main__":
    logger.info("registro-auditoria iniciando en el puerto %d", PUERTO)
    app.run(host="0.0.0.0", port=PUERTO, threaded=True)
