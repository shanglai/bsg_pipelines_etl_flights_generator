"""Conexiones y esquemas de MySQL.

Dos esquemas en la misma instancia:
- `simulador`: interno (reloj, agenda, verdad, colas de publicación, incidentes,
  bitácora de errores, equipos). Nunca se otorga a los alumnos y sus escrituras
  NO van al binlog (sql_log_bin=0 y --binlog-ignore-db=simulador), así que ni con
  privilegios de replicación se puede leer la verdad.
- `aerolinea_c`: la fuente de la aerolínea C, de solo lectura para los alumnos.
"""
from __future__ import annotations

import ssl
import time

import pymysql

from .config import ConexionMySQL

DDL_SIMULADOR = [
    "CREATE DATABASE IF NOT EXISTS simulador CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci",
    """CREATE TABLE IF NOT EXISTS simulador.reloj (
        id TINYINT PRIMARY KEY, inicio_real DOUBLE NOT NULL, inicio_sim DOUBLE NOT NULL,
        factor DOUBLE NOT NULL, semilla BIGINT NOT NULL, creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS simulador.estado (
        clave VARCHAR(64) PRIMARY KEY, valor TEXT, actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS simulador.nacidos (
        aerolinea CHAR(2) NOT NULL, numero_vuelo VARCHAR(8) NOT NULL, fecha_salida DATE NOT NULL,
        creado_sim BIGINT NOT NULL, PRIMARY KEY (aerolinea, numero_vuelo, fecha_salida))""",
    """CREATE TABLE IF NOT EXISTS simulador.agenda (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, t_sim BIGINT NOT NULL, orden INT NOT NULL,
        evento_id VARCHAR(24) NOT NULL, aerolinea CHAR(2) NOT NULL, numero_vuelo VARCHAR(8) NOT NULL,
        fecha_salida DATE NOT NULL, tipo VARCHAR(20) NOT NULL, asiento VARCHAR(4), pasajero VARCHAR(16),
        inyectado VARCHAR(32), datos JSON, KEY k_t (t_sim, orden))""",
    """CREATE TABLE IF NOT EXISTS simulador.verdad_eventos (
        evento_id VARCHAR(24) PRIMARY KEY, aerolinea CHAR(2) NOT NULL, numero_vuelo VARCHAR(8) NOT NULL,
        fecha_salida DATE NOT NULL, tipo VARCHAR(20) NOT NULL, asiento VARCHAR(4), pasajero VARCHAR(16),
        ocurrido_sim BIGINT NOT NULL, orden INT NOT NULL, ocurrido_real DOUBLE NOT NULL,
        KEY k_t (ocurrido_sim), KEY k_v (aerolinea, numero_vuelo, fecha_salida))""",
    """CREATE TABLE IF NOT EXISTS simulador.verdad_vuelos (
        aerolinea CHAR(2) NOT NULL, numero_vuelo VARCHAR(8) NOT NULL, fecha_salida DATE NOT NULL,
        origen CHAR(3) NOT NULL, destino CHAR(3) NOT NULL, salida_local DATETIME NOT NULL,
        llegada_local DATETIME NOT NULL, salida_utc BIGINT NOT NULL, llegada_utc BIGINT NOT NULL,
        estado VARCHAR(12) NOT NULL, creado_sim BIGINT NOT NULL, cancelado_sim BIGINT,
        cerrado_sim BIGINT, PRIMARY KEY (aerolinea, numero_vuelo, fecha_salida), KEY k_c (creado_sim))""",
    """CREATE TABLE IF NOT EXISTS simulador.verdad_asientos (
        aerolinea CHAR(2) NOT NULL, numero_vuelo VARCHAR(8) NOT NULL, fecha_salida DATE NOT NULL,
        asiento VARCHAR(4) NOT NULL, pasajero VARCHAR(16) NOT NULL, asignado_sim BIGINT NOT NULL,
        PRIMARY KEY (aerolinea, numero_vuelo, fecha_salida, asiento))""",
    """CREATE TABLE IF NOT EXISTS simulador.salida (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, fuente CHAR(1) NOT NULL, evento_id VARCHAR(24) NOT NULL,
        evento JSON NOT NULL, ocurrido_real DOUBLE NOT NULL, publicar_en DOUBLE NOT NULL,
        ventana BIGINT, retenido TINYINT NOT NULL DEFAULT 0, duplicado TINYINT NOT NULL DEFAULT 0,
        KEY k_f (fuente, publicar_en))""",
    """CREATE TABLE IF NOT EXISTS simulador.api_eventos (
        seq BIGINT AUTO_INCREMENT PRIMARY KEY, id_evento VARCHAR(24) NOT NULL, version TINYINT NOT NULL,
        payload TEXT NOT NULL, publicado_en DOUBLE NOT NULL, KEY k_p (publicado_en))""",
    """CREATE TABLE IF NOT EXISTS simulador.sftp_archivos (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, nombre VARCHAR(80) NOT NULL, ventana BIGINT NOT NULL,
        tipo VARCHAR(10) NOT NULL, version INT NOT NULL, estado VARCHAR(20) NOT NULL,
        programado_en DOUBLE NOT NULL, renombrar_en DOUBLE, escrito_en DOUBLE,
        codificacion VARCHAR(16), separador VARCHAR(2), filas INT, bytes INT, sha256 CHAR(64),
        defecto VARCHAR(32), KEY k_n (nombre), KEY k_e (estado, programado_en))""",
    """CREATE TABLE IF NOT EXISTS simulador.incidentes (
        id INT AUTO_INCREMENT PRIMARY KEY, tipo VARCHAR(40) NOT NULL, inicio_real DOUBLE NOT NULL,
        fin_real DOUBLE, inicio_sim BIGINT NOT NULL, fin_sim BIGINT, parametros JSON,
        origen VARCHAR(20) NOT NULL, nota TEXT)""",
    """CREATE TABLE IF NOT EXISTS simulador.errores_inyectados (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, fuente CHAR(1) NOT NULL, tipo VARCHAR(40) NOT NULL,
        aerolinea CHAR(2), numero_vuelo VARCHAR(8), fecha_salida DATE, referencia VARCHAR(120),
        detalle JSON, real_en DOUBLE NOT NULL, sim_en BIGINT NOT NULL, KEY k_t (tipo))""",
    """CREATE TABLE IF NOT EXISTS simulador.contadores (
        clave VARCHAR(64) PRIMARY KEY, valor BIGINT NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS simulador.equipos (
        nombre VARCHAR(32) PRIMARY KEY, llave_api_sha256 CHAR(64) NOT NULL,
        limite_por_minuto INT NOT NULL DEFAULT 60, creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
]

DDL_AEROLINEA_C = [
    "CREATE DATABASE IF NOT EXISTS aerolinea_c CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci",
    """CREATE TABLE IF NOT EXISTS aerolinea_c.vuelos (
        id INT AUTO_INCREMENT PRIMARY KEY,
        numero_vuelo VARCHAR(8) NOT NULL COMMENT 'Código de aerolínea + número, p. ej. XJ123',
        fecha_salida DATE NOT NULL COMMENT 'Fecha local de salida en el aeropuerto de origen',
        origen CHAR(4) NOT NULL COMMENT 'Código ICAO',
        destino CHAR(4) NOT NULL COMMENT 'Código ICAO',
        salida_local DATETIME NOT NULL COMMENT 'Hora local del origen, sin zona',
        llegada_local DATETIME NOT NULL COMMENT 'Hora local del destino, sin zona',
        estado VARCHAR(12) NOT NULL COMMENT 'ABIERTO | CANCELADO | CERRADO',
        creado_en DATETIME NOT NULL COMMENT 'UTC',
        actualizado_en DATETIME NOT NULL COMMENT 'UTC',
        KEY k_act (actualizado_en), KEY k_neg (numero_vuelo, fecha_salida))""",
    """CREATE TABLE IF NOT EXISTS aerolinea_c.asignaciones (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        vuelo_id INT NOT NULL COMMENT 'vuelos.id',
        asiento VARCHAR(4) NOT NULL,
        pasajero VARCHAR(16) NOT NULL COMMENT 'Identificador opaco',
        asignado_en DATETIME NOT NULL COMMENT 'UTC',
        actualizado_en DATETIME NOT NULL COMMENT 'UTC',
        KEY k_v (vuelo_id), KEY k_act (actualizado_en))""",
]


def conectar(c: ConexionMySQL | None = None, base: str | None = None, binlog: bool = True,
             reintentos: int = 60) -> pymysql.connections.Connection:
    c = c or ConexionMySQL.desde_entorno()
    contexto = None
    if c.tls:
        contexto = ssl.create_default_context()
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE   # certificado autofirmado de MySQL dentro de la red de Docker
    ultimo: Exception | None = None
    for _ in range(reintentos):
        try:
            cn = pymysql.connect(host=c.host, port=c.puerto, user=c.usuario, password=c.contrasena,
                                 database=base, autocommit=False, charset="utf8mb4", ssl=contexto,
                                 cursorclass=pymysql.cursors.DictCursor, connect_timeout=10)
            if not binlog:
                with cn.cursor() as cur:
                    cur.execute("SET SESSION sql_log_bin = 0")
            return cn
        except pymysql.err.OperationalError as e:
            ultimo = e
            time.sleep(2)
    raise RuntimeError(f"No pude conectar a MySQL en {c.host}:{c.puerto}: {ultimo}")


def crear_esquemas(cn_interna, cn_fuente) -> None:
    with cn_interna.cursor() as cur:
        for s in DDL_SIMULADOR:
            cur.execute(s)
    cn_interna.commit()
    with cn_fuente.cursor() as cur:
        for s in DDL_AEROLINEA_C:
            cur.execute(s)
    cn_fuente.commit()


def leer_estado(cn, clave: str, omision: str | None = None) -> str | None:
    with cn.cursor() as cur:
        cur.execute("SELECT valor FROM simulador.estado WHERE clave=%s", (clave,))
        fila = cur.fetchone()
    return fila["valor"] if fila else omision


def escribir_estado(cn, clave: str, valor: object) -> None:
    with cn.cursor() as cur:
        cur.execute("INSERT INTO simulador.estado (clave, valor) VALUES (%s,%s) "
                    "ON DUPLICATE KEY UPDATE valor=VALUES(valor)", (clave, str(valor)))


def sumar_contadores(cn, contadores: dict[str, int]) -> None:
    filas = [(k, v) for k, v in contadores.items() if v]
    if not filas:
        return
    with cn.cursor() as cur:
        cur.executemany("INSERT INTO simulador.contadores (clave, valor) VALUES (%s,%s) "
                        "ON DUPLICATE KEY UPDATE valor = valor + VALUES(valor)", filas)
