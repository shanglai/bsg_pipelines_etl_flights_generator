"""Consumidor MÍNIMO de prueba (no es el pipeline de los alumnos).

Lee las tres fuentes completas y escribe una entrega en DuckDB con el esquema que espera el
calificador. Es deliberadamente ingenuo: maneja lo indispensable para el modo LIMPIO
(ordenar por momento del evento, convertir zonas horarias, ICAO -> IATA, 429/5xx) y NO
maneja los errores inyectados (duplicados, reglas de negocio, v2, codificaciones,
separadores, truncados, filas borradas, transacciones largas). Así sirve para demostrar
que el calificador da 100 % en modo limpio y detecta diferencias con errores.

    python validacion/consumidor_minimo.py --api http://localhost:8000 --llave ... \
        --sftp-host localhost --sftp-puerto 2222 --sftp-usuario alumno --sftp-contrasena ... \
        --mysql-host 127.0.0.1 --mysql-puerto 3307 --mysql-usuario alumno --mysql-contrasena ... \
        --salida entregas/minimo.duckdb
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import paramiko
import pymysql
import requests

RAIZ = Path(__file__).resolve().parents[1]


def aeropuertos() -> dict[str, dict]:
    with (RAIZ / "datos/aeropuertos.csv").open(encoding="utf-8") as f:
        return {r["iata"]: r for r in csv.DictReader(f)}


def a_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------- A: API
def leer_api(base: str, llave: str, stats: Counter) -> list[dict]:
    eventos, cursor = [], None
    s = requests.Session()
    s.headers["X-API-Key"] = llave
    while True:
        params = {"limite": 500}
        if cursor:
            params["cursor"] = cursor
        r = s.get(f"{base}/v1/eventos", params=params, timeout=30)
        if r.status_code == 429:
            stats["api_429"] += 1
            time.sleep(float(r.headers.get("Retry-After", "1")))
            continue
        if r.status_code >= 500:
            stats[f"api_{r.status_code}"] += 1
            time.sleep(1)
            continue
        r.raise_for_status()
        d = r.json()
        eventos.extend(d["datos"])
        stats["api_paginas"] += 1
        cursor = d["siguiente_cursor"]
        if not d["hay_mas"]:
            return eventos


def estado_desde_api(eventos: list[dict], aps) -> tuple[dict, dict]:
    vuelos, asientos = {}, {}
    orden = sorted(enumerate(eventos), key=lambda x: (datetime.fromisoformat(x[1]["ocurrido_en"].replace("Z", "+00:00")), x[0]))
    for _, e in orden:
        k = (e["aerolinea"], e["numero_vuelo"], e["fecha_salida_local"])
        t = e["tipo"]
        if t == "vuelo_creado":
            vuelos[k] = {"origen": e["origen"], "destino": e["destino"],
                         "salida_utc": datetime.fromisoformat(e["salida_programada_utc"].replace("Z", "+00:00")).replace(tzinfo=None),
                         "estado": "programado"}
        elif t in ("vuelo_cancelado", "vuelo_cerrado"):
            if k in vuelos:
                vuelos[k]["estado"] = "cancelado" if t == "vuelo_cancelado" else "cerrado"
            if t == "vuelo_cancelado":
                for kk in [x for x in asientos if x[:3] == k]:
                    del asientos[kk]
        elif t == "asiento_asignado":
            asientos[(*k, e["asiento"])] = e.get("pasajero")
        elif t == "asiento_liberado":
            asientos.pop((*k, e["asiento"]), None)
    return vuelos, asientos


# ----------------------------------------------------------------------------- B: SFTP
def leer_sftp(host, puerto, usuario, contrasena, stats: Counter) -> dict[str, bytes]:
    t = paramiko.Transport((host, puerto))
    t.connect(username=usuario, password=contrasena)
    sftp = paramiko.SFTPClient.from_transport(t)
    archivos = {}
    for nombre in sorted(sftp.listdir("salida")):
        if nombre.startswith(".") or not nombre.endswith(".csv"):
            stats["sftp_ignorados"] += 1
            continue
        with sftp.open(f"salida/{nombre}", "rb") as fh:
            archivos[nombre] = fh.read()
    sftp.close()
    t.close()
    stats["sftp_archivos"] = len(archivos)
    return archivos


def estado_desde_sftp(archivos: dict[str, bytes], aps, codigo: str, stats: Counter) -> tuple[dict, dict]:
    def ddmm(fecha: str, hora: str = "00:00:00") -> datetime:
        return datetime.strptime(f"{fecha} {hora}", "%d/%m/%Y %H:%M:%S" if hora.count(":") == 2 else "%d/%m/%Y %H:%M")

    filas_v, filas_a = [], []
    origen_de: dict[str, str] = {}
    for nombre, b in archivos.items():
        try:
            texto = b.decode("utf-8")
            filas = list(csv.DictReader(io.StringIO(texto)))
        except (UnicodeDecodeError, csv.Error):
            stats["sftp_ilegibles"] += 1
            continue
        if "_vuelos_" in nombre:
            if filas and "estado" not in filas[0]:
                stats["sftp_ilegibles"] += 1
                continue
            filas_v += filas
        else:
            if filas and "movimiento" not in filas[0]:
                stats["sftp_ilegibles"] += 1
                continue
            filas_a += filas
    vuelos = {}
    for f in filas_v:
        try:
            fecha = ddmm(f["fecha_salida"]).date().isoformat()
            act = a_utc(ddmm(*f["actualizado"].split(" ")), aps[f["origen"]]["zona_horaria"])
            k = (codigo, f["numero_vuelo"], fecha)
            origen_de[f["numero_vuelo"]] = f["origen"]
            estado = {"PROGRAMADO": "programado", "CANCELADO": "cancelado", "CERRADO": "cerrado"}[f["estado"]]
            if k not in vuelos or act >= vuelos[k]["_act"]:
                vuelos[k] = {"origen": f["origen"], "destino": f["destino"], "estado": estado, "_act": act,
                             "salida_utc": a_utc(ddmm(f["fecha_salida"], f["hora_salida"]), aps[f["origen"]]["zona_horaria"])}
        except (KeyError, ValueError, TypeError):
            stats["sftp_filas_malas"] += 1
    movs = []
    for f in filas_a:
        try:
            tz = aps[origen_de[f["numero_vuelo"]]]["zona_horaria"]
            movs.append((a_utc(ddmm(*f["fecha_hora_movimiento"].split(" ")), tz), f))
        except (KeyError, ValueError, TypeError):
            stats["sftp_filas_malas"] += 1
    asientos = {}
    for _, f in sorted(movs, key=lambda x: x[0]):
        k = (codigo, f["numero_vuelo"], ddmm(f["fecha_salida"]).date().isoformat(), f["asiento"])
        if f["movimiento"] == "ASIGNACION":
            asientos[k] = f["pasajero"]
        else:
            asientos.pop(k, None)
    for k, v in vuelos.items():
        v.pop("_act")
        if v["estado"] == "cancelado":
            for kk in [x for x in asientos if x[:3] == k]:
                del asientos[kk]
    return vuelos, asientos


# ----------------------------------------------------------------------------- C: MySQL
def leer_mysql(host, puerto, usuario, contrasena, aps, codigo: str) -> tuple[dict, dict]:
    por_icao = {a["icao"]: a for a in aps.values()}
    cn = pymysql.connect(host=host, port=puerto, user=usuario, password=contrasena, database="aerolinea_c",
                         cursorclass=pymysql.cursors.DictCursor)
    with cn.cursor() as cur:
        cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        cur.execute("SELECT * FROM vuelos")
        vs = cur.fetchall()
        cur.execute("SELECT * FROM asignaciones")
        asg = cur.fetchall()
    cn.close()
    vuelos, por_id = {}, {}
    for v in vs:
        o, d = por_icao[v["origen"]], por_icao[v["destino"]]
        k = (codigo, v["numero_vuelo"], v["fecha_salida"].isoformat())
        por_id[v["id"]] = k
        vuelos[k] = {"origen": o["iata"], "destino": d["iata"], "salida_utc": a_utc(v["salida_local"], o["zona_horaria"]),
                     "estado": {"ABIERTO": "programado", "CANCELADO": "cancelado", "CERRADO": "cerrado"}[v["estado"]]}
    asientos = {(*por_id[a["vuelo_id"]], a["asiento"]): a["pasajero"] for a in asg if a["vuelo_id"] in por_id}
    return vuelos, asientos


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--llave", required=True)
    ap.add_argument("--sftp-host", default="localhost")
    ap.add_argument("--sftp-puerto", type=int, default=2222)
    ap.add_argument("--sftp-usuario", default="alumno")
    ap.add_argument("--sftp-contrasena", required=True)
    ap.add_argument("--mysql-host", default="127.0.0.1")
    ap.add_argument("--mysql-puerto", type=int, default=3307)
    ap.add_argument("--mysql-usuario", default="alumno")
    ap.add_argument("--mysql-contrasena", required=True)
    ap.add_argument("--salida", required=True)
    a = ap.parse_args()

    aps = aeropuertos()
    stats: Counter = Counter()
    salud = requests.get(f"{a.api}/salud", timeout=10).json()
    corte = salud["reloj_simulado"]
    t0 = time.time()
    va, sa = estado_desde_api(leer_api(a.api, a.llave, stats), aps)
    vb, sb = estado_desde_sftp(leer_sftp(a.sftp_host, a.sftp_puerto, a.sftp_usuario, a.sftp_contrasena, stats), aps, "JG", stats)
    vc, sc = leer_mysql(a.mysql_host, a.mysql_puerto, a.mysql_usuario, a.mysql_contrasena, aps, "XJ")
    vuelos = {**va, **vb, **vc}
    asientos = {**sa, **sb, **sc}

    salida = Path(a.salida)
    salida.unlink(missing_ok=True)
    con = duckdb.connect(str(salida))
    con.execute("CREATE TABLE entrega_vuelos (aerolinea VARCHAR, numero_vuelo VARCHAR, fecha_salida_local DATE, "
                "origen VARCHAR, destino VARCHAR, salida_programada_utc TIMESTAMP, estado VARCHAR)")
    con.execute("CREATE TABLE entrega_asientos (aerolinea VARCHAR, numero_vuelo VARCHAR, fecha_salida_local DATE, "
                "asiento VARCHAR, pasajero VARCHAR)")
    con.executemany("INSERT INTO entrega_vuelos VALUES (?,?,?,?,?,?,?)",
                    [(*k, v["origen"], v["destino"], v["salida_utc"], v["estado"]) for k, v in vuelos.items()])
    con.executemany("INSERT INTO entrega_asientos VALUES (?,?,?,?,?)", [(*k, p) for k, p in asientos.items()])
    con.close()
    print(json.dumps({"corte_simulado": corte, "segundos": round(time.time() - t0, 1), "vuelos": len(vuelos),
                      "asientos": len(asientos), "por_fuente": {"A": [len(va), len(sa)], "B": [len(vb), len(sb)],
                                                                "C": [len(vc), len(sc)]}, **stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
