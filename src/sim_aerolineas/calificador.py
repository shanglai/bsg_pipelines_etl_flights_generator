"""Calificador de COMPLETITUD: compara la entrega de un equipo contra la verdad.

Entrega (ver docs/esquema_entrega.md): una base DuckDB con las tablas `entrega_vuelos` y
`entrega_asientos`, o una carpeta con `entrega_vuelos.parquet` y `entrega_asientos.parquet`.

Reglas de comparación
- Corte: momento simulado contra el que se califica (por omisión, el más reciente de la verdad).
- Margen: tiempo para que los datos lleguen (latencia de las fuentes). Se expresa en minutos
  REALES y se convierte con el factor del reloj. Un vuelo creado después de (corte - margen)
  no se exige; un estado se acepta si coincide con el de (corte - margen), el del corte o el
  más reciente conocido.
- Asientos: solo de vuelos cerrados o cancelados antes de (corte - margen). Son definitivos.
  Un vuelo cancelado debe quedar sin asientos.
- Llave de vuelo: (aerolinea, numero_vuelo, fecha_salida_local). El número se normaliza
  (mayúsculas, sin espacios ni ceros a la izquierda: "xj 0123" -> "XJ123").
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

ESTADOS = ("programado", "cancelado", "cerrado")


@dataclass
class Parametros:
    corte_sim: int
    margen_sim: int
    hasta_sim: int


def _norm_num(col: str) -> str:
    c = f"upper(replace(trim(CAST({col} AS VARCHAR)), ' ', ''))"
    return f"(left({c}, 2) || coalesce(nullif(ltrim(substr({c}, 3), '0'), ''), '0'))"


def cargar_entrega(con: duckdb.DuckDBPyConnection, ruta: Path) -> None:
    ruta = Path(ruta)
    if ruta.is_dir():
        fv, fa = (ruta / "entrega_vuelos.parquet").as_posix(), (ruta / "entrega_asientos.parquet").as_posix()
        con.execute(f"CREATE OR REPLACE VIEW _ev AS SELECT * FROM '{fv}'")
        con.execute(f"CREATE OR REPLACE VIEW _ea AS SELECT * FROM '{fa}'")
    else:
        con.execute(f"ATTACH '{ruta.as_posix()}' AS entrega (READ_ONLY)")
        con.execute("CREATE OR REPLACE VIEW _ev AS SELECT * FROM entrega.entrega_vuelos")
        con.execute("CREATE OR REPLACE VIEW _ea AS SELECT * FROM entrega.entrega_asientos")
    con.execute(f"""CREATE OR REPLACE TABLE e_vuelos AS SELECT
        upper(trim(aerolinea)) aerolinea, {_norm_num('numero_vuelo')} numero_vuelo,
        CAST(fecha_salida_local AS DATE) fecha_salida, upper(trim(origen)) origen, upper(trim(destino)) destino,
        CAST(salida_programada_utc AS TIMESTAMP) salida_programada_utc, lower(trim(estado)) estado FROM _ev""")
    con.execute(f"""CREATE OR REPLACE TABLE e_asientos AS SELECT
        upper(trim(aerolinea)) aerolinea, {_norm_num('numero_vuelo')} numero_vuelo,
        CAST(fecha_salida_local AS DATE) fecha_salida, upper(trim(asiento)) asiento, trim(pasajero) pasajero
        FROM _ea""")


def calificar(con: duckdb.DuckDBPyConnection, p: Parametros) -> dict:
    """Requiere en `con`: v_vuelos, v_asientos_cierre (verdad) y e_vuelos, e_asientos (entrega)."""
    lim = p.corte_sim - p.margen_sim

    def est(x: str) -> str:
        return (f"CASE WHEN cancelado_sim <= {x} THEN 'cancelado' WHEN cerrado_sim <= {x} THEN 'cerrado' "
                f"ELSE 'programado' END")

    con.execute(f"""CREATE OR REPLACE TABLE c_vuelos AS
        WITH v AS (SELECT *, {est(str(lim))} e_lim, {est(str(p.corte_sim))} e_corte, {est(str(p.hasta_sim))} e_hasta,
                          creado_sim <= {lim} AS exigible FROM v_vuelos),
             e AS (SELECT *, count(*) OVER (PARTITION BY aerolinea, numero_vuelo, fecha_salida) n_dup,
                          row_number() OVER (PARTITION BY aerolinea, numero_vuelo, fecha_salida ORDER BY estado) rn
                   FROM e_vuelos)
        SELECT coalesce(v.aerolinea, e.aerolinea) aerolinea, coalesce(v.numero_vuelo, e.numero_vuelo) numero_vuelo,
               coalesce(v.fecha_salida, e.fecha_salida) fecha_salida,
               v.aerolinea IS NOT NULL en_verdad, e.aerolinea IS NOT NULL en_entrega, coalesce(v.exigible, false) exigible,
               e.estado estado_entrega, v.e_corte estado_verdad,
               e.estado IN (v.e_lim, v.e_corte, v.e_hasta) estado_ok,
               (e.origen IS DISTINCT FROM v.origen OR e.destino IS DISTINCT FROM v.destino
                OR e.salida_programada_utc IS DISTINCT FROM CAST(to_timestamp(v.salida_utc) AT TIME ZONE 'UTC' AS TIMESTAMP))
                 AS atributos_distintos,
               coalesce(e.n_dup, 0) n_dup
        FROM v FULL OUTER JOIN (SELECT * FROM e WHERE rn = 1) e USING (aerolinea, numero_vuelo, fecha_salida)""")

    con.execute(f"""CREATE OR REPLACE TABLE c_vuelos_asientos AS
        SELECT aerolinea, numero_vuelo, fecha_salida FROM v_vuelos
        WHERE cerrado_sim <= {lim} OR cancelado_sim <= {lim}""")
    con.execute("""CREATE OR REPLACE TABLE c_asientos AS
        WITH t AS (SELECT a.* FROM v_asientos_cierre a JOIN c_vuelos_asientos USING (aerolinea, numero_vuelo, fecha_salida)),
             e AS (SELECT a.*, count(*) OVER (PARTITION BY aerolinea, numero_vuelo, fecha_salida, asiento) n_dup,
                          row_number() OVER (PARTITION BY aerolinea, numero_vuelo, fecha_salida, asiento ORDER BY pasajero) rn
                   FROM e_asientos a JOIN c_vuelos_asientos USING (aerolinea, numero_vuelo, fecha_salida))
        SELECT coalesce(t.aerolinea, e.aerolinea) aerolinea, coalesce(t.numero_vuelo, e.numero_vuelo) numero_vuelo,
               coalesce(t.fecha_salida, e.fecha_salida) fecha_salida, coalesce(t.asiento, e.asiento) asiento,
               t.pasajero pasajero_verdad, e.pasajero pasajero_entrega, coalesce(e.n_dup, 0) n_dup,
               CASE WHEN t.asiento IS NULL THEN 'sobrante' WHEN e.asiento IS NULL THEN 'faltante'
                    WHEN t.pasajero = e.pasajero THEN 'correcto' ELSE 'conflicto' END resultado
        FROM t FULL OUTER JOIN (SELECT * FROM e WHERE rn = 1) e USING (aerolinea, numero_vuelo, fecha_salida, asiento)""")

    vuelos = con.execute("""
        SELECT aerolinea,
          count(*) FILTER (WHERE en_verdad AND exigible) esperados,
          count(*) FILTER (WHERE en_verdad AND exigible AND en_entrega) encontrados,
          count(*) FILTER (WHERE en_verdad AND exigible AND NOT en_entrega) faltantes,
          count(*) FILTER (WHERE en_entrega AND NOT en_verdad) sobrantes,
          count(*) FILTER (WHERE en_verdad AND exigible AND en_entrega AND NOT estado_ok) estado_distinto,
          count(*) FILTER (WHERE en_verdad AND exigible AND en_entrega AND estado_ok) correctos,
          count(*) FILTER (WHERE en_verdad AND en_entrega AND atributos_distintos) atributos_distintos,
          count(*) FILTER (WHERE en_verdad AND NOT exigible AND en_entrega) dentro_del_margen,
          sum(CASE WHEN n_dup > 1 THEN n_dup - 1 ELSE 0 END) llaves_duplicadas
        FROM c_vuelos GROUP BY aerolinea ORDER BY aerolinea""").fetchall()
    cols_v = ["aerolinea", "esperados", "encontrados", "faltantes", "sobrantes", "estado_distinto", "correctos",
              "atributos_distintos", "dentro_del_margen", "llaves_duplicadas"]
    asientos = con.execute("""
        SELECT aerolinea,
          count(*) FILTER (WHERE pasajero_verdad IS NOT NULL) esperados,
          count(*) FILTER (WHERE resultado='correcto') correctos,
          count(*) FILTER (WHERE resultado='faltante') faltantes,
          count(*) FILTER (WHERE resultado='sobrante') sobrantes,
          count(*) FILTER (WHERE resultado='conflicto') conflicto,
          sum(CASE WHEN n_dup > 1 THEN n_dup - 1 ELSE 0 END) duplicados
        FROM c_asientos GROUP BY aerolinea ORDER BY aerolinea""").fetchall()
    cols_a = ["aerolinea", "esperados", "correctos", "faltantes", "sobrantes", "conflicto", "duplicados"]
    nvc = con.execute("""SELECT aerolinea, count(*) FROM c_vuelos_asientos GROUP BY 1""").fetchall()
    vuelos_con_asientos = dict(nvc)

    res = {"parametros": {"corte_sim_epoch": p.corte_sim, "margen_sim_s": p.margen_sim, "hasta_sim_epoch": p.hasta_sim},
           "por_aerolinea": {}, "total": {}}
    for f in vuelos:
        d = dict(zip(cols_v, f))
        res["por_aerolinea"].setdefault(d.pop("aerolinea"), {})["vuelos"] = d
    for f in asientos:
        d = dict(zip(cols_a, f))
        a = d.pop("aerolinea")
        d["vuelos_evaluados"] = vuelos_con_asientos.get(a, 0)
        res["por_aerolinea"].setdefault(a, {})["asientos"] = d
    tot_v: dict = {}
    tot_a: dict = {}
    for a, d in res["por_aerolinea"].items():
        for k, v in d.get("vuelos", {}).items():
            tot_v[k] = tot_v.get(k, 0) + (v or 0)
        for k, v in d.get("asientos", {}).items():
            tot_a[k] = tot_a.get(k, 0) + (v or 0)
    res["total"] = {"vuelos": tot_v, "asientos": tot_a}
    for d in [*res["por_aerolinea"].values(), res["total"]]:
        v, s = d.get("vuelos", {}), d.get("asientos", {})
        if v:
            v["completitud"] = round(v["correctos"] / v["esperados"], 6) if v.get("esperados") else None
        if s:
            s["completitud"] = round(s["correctos"] / s["esperados"], 6) if s.get("esperados") else None
    return res


def texto(res: dict) -> str:
    from datetime import datetime, timezone
    p = res["parametros"]
    f = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    out = ["CALIFICADOR DE COMPLETITUD", "=" * 96,
           f"Corte simulado: {f(p['corte_sim_epoch'])} UTC   margen: {p['margen_sim_s'] / 3600:.1f} h simuladas   "
           f"verdad conocida hasta: {f(p['hasta_sim_epoch'])} UTC", "",
           "VUELOS", "-" * 96,
           f"{'aerolínea':<10}{'esperados':>10}{'correctos':>10}{'faltantes':>10}{'sobrantes':>10}{'estado≠':>9}"
           f"{'atrib≠':>8}{'dup':>6}{'margen':>8}{'completitud':>14}"]

    def pct(x):
        return "-" if x is None else f"{100 * x:.2f} %"

    filas = list(res["por_aerolinea"].items()) + [("TOTAL", res["total"])]
    for a, d in filas:
        v = d.get("vuelos")
        if v:
            out.append(f"{a:<10}{v['esperados']:>10}{v['correctos']:>10}{v['faltantes']:>10}{v['sobrantes']:>10}"
                       f"{v['estado_distinto']:>9}{v['atributos_distintos']:>8}{v['llaves_duplicadas']:>6}"
                       f"{v['dentro_del_margen']:>8}{pct(v['completitud']):>14}")
    out += ["", "ASIENTOS AL CIERRE (vuelos cerrados o cancelados antes de corte - margen)", "-" * 96,
            f"{'aerolínea':<10}{'vuelos':>8}{'esperados':>10}{'correctos':>10}{'faltantes':>10}{'sobrantes':>10}"
            f"{'conflicto':>10}{'dup':>6}{'completitud':>14}"]
    for a, d in filas:
        s = d.get("asientos")
        if s:
            out.append(f"{a:<10}{s.get('vuelos_evaluados', 0):>8}{s['esperados']:>10}{s['correctos']:>10}"
                       f"{s['faltantes']:>10}{s['sobrantes']:>10}{s['conflicto']:>10}{s['duplicados']:>6}"
                       f"{pct(s['completitud']):>14}")
    return "\n".join(out) + "\n"


def exportar_detalle(con: duckdb.DuckDBPyConnection, carpeta: Path) -> None:
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT * FROM c_vuelos WHERE (en_verdad AND exigible AND (NOT en_entrega OR NOT estado_ok))
                    OR (en_entrega AND NOT en_verdad) OR atributos_distintos ORDER BY 1,2,3)
                    TO '{(carpeta / 'diferencias_vuelos.csv').as_posix()}' (HEADER)""")
    con.execute(f"""COPY (SELECT * FROM c_asientos WHERE resultado <> 'correcto' ORDER BY 1,2,3,4)
                    TO '{(carpeta / 'diferencias_asientos.csv').as_posix()}' (HEADER)""")


def correr(entrega: Path, *, cn=None, reloj=None, verdad_dir: Path | None = None, corte_sim: int | None = None,
           margen_min_real: float = 15.0, detalle: Path | None = None) -> dict:
    from . import verdad
    con = duckdb.connect()
    if verdad_dir:
        meta = verdad.cargar_exportada(con, verdad_dir)
        hasta, factor = int(meta["hasta_sim_epoch"]), float(meta["factor"])
    else:
        from . import bd
        hasta = int(float(bd.leer_estado(cn, "sim_ahora", "0")))
        factor = reloj.factor
        verdad.cargar_en_duckdb(cn, con, hasta)
    corte = int(corte_sim or hasta)
    p = Parametros(corte_sim=corte, margen_sim=int(margen_min_real * 60 * factor), hasta_sim=max(hasta, corte))
    cargar_entrega(con, entrega)
    res = calificar(con, p)
    res["parametros"]["margen_min_real"] = margen_min_real
    if detalle:
        exportar_detalle(con, detalle)
        (Path(detalle) / "resultado.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res
