"""La verdad canónica (solo instructor): lectura desde MySQL y exportación a Parquet."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

from .reloj import Reloj


def _tabla(cn, sql: str, params=()) -> pa.Table:
    with cn.cursor() as cur:
        cur.execute(sql, params)
        filas = cur.fetchall()
        columnas = [d[0] for d in cur.description] if cur.description else []
    if not filas:
        return pa.table({c: pa.array([], type=pa.string()) for c in columnas})
    return pa.Table.from_pylist(filas)


def cargar_en_duckdb(cn, con: duckdb.DuckDBPyConnection, hasta_sim: int) -> None:
    """Carga en DuckDB las tablas v_vuelos, v_asientos_cierre y v_eventos (hasta `hasta_sim`).

    Los momentos se guardan como epoch (segundos) simulados.
    """
    vuelos = _tabla(cn, """
        SELECT aerolinea, numero_vuelo, fecha_salida, origen, destino, salida_local, llegada_local,
               salida_utc, llegada_utc, creado_sim,
               CASE WHEN cancelado_sim <= %s THEN cancelado_sim END AS cancelado_sim,
               CASE WHEN cerrado_sim <= %s THEN cerrado_sim END AS cerrado_sim
        FROM simulador.verdad_vuelos WHERE creado_sim <= %s""", (hasta_sim, hasta_sim, hasta_sim))
    asientos = _tabla(cn, """
        SELECT a.aerolinea, a.numero_vuelo, a.fecha_salida, a.asiento, a.pasajero, a.asignado_sim
        FROM simulador.verdad_asientos a JOIN simulador.verdad_vuelos v
          USING (aerolinea, numero_vuelo, fecha_salida)
        WHERE v.cerrado_sim <= %s""", (hasta_sim,))
    con.register("_vv", vuelos)
    con.register("_va", asientos)
    con.execute("""CREATE OR REPLACE TABLE v_vuelos AS SELECT aerolinea, numero_vuelo,
                   CAST(fecha_salida AS DATE) fecha_salida, origen, destino,
                   CAST(salida_local AS TIMESTAMP) salida_local, CAST(llegada_local AS TIMESTAMP) llegada_local,
                   CAST(salida_utc AS BIGINT) salida_utc, CAST(llegada_utc AS BIGINT) llegada_utc,
                   CAST(creado_sim AS BIGINT) creado_sim, CAST(cancelado_sim AS BIGINT) cancelado_sim,
                   CAST(cerrado_sim AS BIGINT) cerrado_sim FROM _vv""")
    con.execute("""CREATE OR REPLACE TABLE v_asientos_cierre AS SELECT aerolinea, numero_vuelo,
                   CAST(fecha_salida AS DATE) fecha_salida, asiento, pasajero, CAST(asignado_sim AS BIGINT) asignado_sim
                   FROM _va""")
    con.unregister("_vv")
    con.unregister("_va")


def exportar(cn, reloj: Reloj, hasta_sim: int, salida: Path) -> dict:
    """Exporta la verdad hasta `hasta_sim` a Parquet en la carpeta `salida`."""
    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    cargar_en_duckdb(cn, con, hasta_sim)
    eventos = _tabla(cn, "SELECT * FROM simulador.verdad_eventos WHERE ocurrido_sim <= %s", (hasta_sim,))
    con.register("_ve", eventos)
    con.execute("""CREATE TABLE v_eventos AS SELECT evento_id, aerolinea, numero_vuelo, CAST(fecha_salida AS DATE)
                   fecha_salida, tipo, asiento, pasajero, CAST(ocurrido_sim AS BIGINT) ocurrido_sim,
                   CAST(orden AS INTEGER) orden, ocurrido_real FROM _ve""")
    con.register("_inc", _tabla(cn, "SELECT id, tipo, inicio_real, fin_real, inicio_sim, fin_sim, "
                                    "CAST(parametros AS CHAR) parametros, origen, nota FROM simulador.incidentes "
                                    "WHERE inicio_sim <= %s", (hasta_sim,)))
    con.register("_err", _tabla(cn, "SELECT id, fuente, tipo, aerolinea, numero_vuelo, fecha_salida, referencia, "
                                    "CAST(detalle AS CHAR) detalle, real_en, sim_en FROM simulador.errores_inyectados "
                                    "WHERE sim_en <= %s", (hasta_sim,)))

    def ts(col: str) -> str:
        return f"CAST(to_timestamp({col}) AT TIME ZONE 'UTC' AS TIMESTAMP)"

    h = int(hasta_sim)
    consultas = {
        "eventos": f"SELECT * EXCLUDE (ocurrido_sim), {ts('ocurrido_sim')} AS ocurrido_en_utc FROM v_eventos "
                   "ORDER BY ocurrido_sim, orden",
        "vuelos": f"""SELECT aerolinea, numero_vuelo, fecha_salida AS fecha_salida_local, origen, destino,
                      salida_local, llegada_local, {ts('salida_utc')} AS salida_programada_utc,
                      {ts('llegada_utc')} AS llegada_programada_utc, {ts('creado_sim')} AS creado_en_utc,
                      {ts('cancelado_sim')} AS cancelado_en_utc, {ts('cerrado_sim')} AS cerrado_en_utc,
                      CASE WHEN cancelado_sim IS NOT NULL THEN 'cancelado' WHEN cerrado_sim IS NOT NULL THEN 'cerrado'
                           ELSE 'programado' END AS estado
                      FROM v_vuelos ORDER BY fecha_salida, aerolinea, numero_vuelo""",
        "asientos_al_cierre": "SELECT aerolinea, numero_vuelo, fecha_salida AS fecha_salida_local, asiento, pasajero "
                              "FROM v_asientos_cierre ORDER BY 1,2,3,4",
        # Asientos vigentes en `hasta` para todos los vuelos no cancelados (reconstrucción por eventos).
        "asientos_vigentes": f"""
            WITH u AS (
              SELECT *, row_number() OVER (PARTITION BY aerolinea, numero_vuelo, fecha_salida, asiento
                                           ORDER BY ocurrido_sim DESC, orden DESC) rn
              FROM v_eventos WHERE tipo IN ('asiento_asignado','asiento_liberado'))
            SELECT u.aerolinea, u.numero_vuelo, u.fecha_salida AS fecha_salida_local, u.asiento, u.pasajero
            FROM u JOIN v_vuelos v USING (aerolinea, numero_vuelo, fecha_salida)
            WHERE rn = 1 AND tipo = 'asiento_asignado' AND v.cancelado_sim IS NULL ORDER BY 1,2,3,4""",
        "incidentes": "SELECT * FROM _inc ORDER BY id",
        "errores_inyectados": "SELECT * FROM _err ORDER BY id",
    }
    conteos = {}
    for nombre, sql in consultas.items():
        destino = salida / f"{nombre}.parquet"
        con.execute(f"COPY ({sql}) TO '{destino.as_posix()}' (FORMAT PARQUET)")
        conteos[nombre] = con.execute(f"SELECT COUNT(*) FROM '{destino.as_posix()}'").fetchone()[0]
    meta = {"hasta_sim_utc": datetime.fromtimestamp(h, tz=timezone.utc).isoformat(), "hasta_sim_epoch": h,
            "factor": reloj.factor, "inicio_sim_epoch": reloj.inicio_sim, "inicio_real_epoch": reloj.inicio_real,
            "conteos": conteos}
    (salida / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def cargar_exportada(con: duckdb.DuckDBPyConnection, carpeta: Path) -> dict:
    """Carga una verdad exportada (Parquet) en las mismas tablas que `cargar_en_duckdb`."""
    carpeta = Path(carpeta)
    meta = json.loads((carpeta / "meta.json").read_text(encoding="utf-8"))
    p = lambda n: (carpeta / f"{n}.parquet").as_posix()  # noqa: E731
    con.execute(f"""CREATE OR REPLACE TABLE v_vuelos AS SELECT aerolinea, numero_vuelo,
        fecha_salida_local AS fecha_salida, origen, destino, salida_local, llegada_local,
        epoch(salida_programada_utc)::BIGINT salida_utc, epoch(llegada_programada_utc)::BIGINT llegada_utc,
        epoch(creado_en_utc)::BIGINT creado_sim, epoch(cancelado_en_utc)::BIGINT cancelado_sim,
        epoch(cerrado_en_utc)::BIGINT cerrado_sim FROM '{p('vuelos')}'""")
    con.execute(f"""CREATE OR REPLACE TABLE v_asientos_cierre AS SELECT aerolinea, numero_vuelo,
        fecha_salida_local AS fecha_salida, asiento, pasajero, NULL::BIGINT asignado_sim
        FROM '{p('asientos_al_cierre')}'""")
    return meta
