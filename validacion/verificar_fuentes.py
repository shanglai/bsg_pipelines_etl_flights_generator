"""Verifica que cada fuente entrega lo esperado y que cada tipo de error aparece con su frecuencia.

Tres miradas independientes:
1. PLAN: recalcula los planes deterministas de los vuelos nacidos y cuenta los errores de negocio.
2. BITÁCORA: lo que el simulador dice que inyectó (simulador.errores_inyectados, con root).
3. FUENTE: lo que un alumno realmente ve (API, SFTP y MySQL con credenciales de alumno).

    python validacion/verificar_fuentes.py --monitor validacion/salidas/monitor.jsonl \
        --salida validacion/salidas/verificacion.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import paramiko
import pymysql
import requests

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
from sim_aerolineas import catalogo, config, plan  # noqa: E402
from sim_aerolineas.catalogo import ASIENTOS  # noqa: E402


def leer_api(base, llave, stats):
    evs, cursor = [], None
    while True:
        r = requests.get(f"{base}/v1/eventos", headers={"X-API-Key": llave},
                         params={"limite": 500, **({"cursor": cursor} if cursor else {})}, timeout=30)
        stats[f"http_{r.status_code}"] += 1
        if r.status_code == 429:
            stats["retry_after_presente"] += "Retry-After" in r.headers
            time.sleep(float(r.headers.get("Retry-After", 1)))
            continue
        if r.status_code >= 500:
            time.sleep(1)
            continue
        d = r.json()
        evs += d["datos"]
        cursor = d["siguiente_cursor"]
        if not d["hay_mas"]:
            return evs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--llave", default="llave-local-123")
    ap.add_argument("--mysql-puerto", type=int, default=3307)
    ap.add_argument("--sftp-puerto", type=int, default=2222)
    ap.add_argument("--root", default="raiz-local-123")
    ap.add_argument("--alumno", default="alumno-local-123")
    ap.add_argument("--monitor")
    ap.add_argument("--salida", required=True)
    ap.add_argument("--rapido", action="store_true", help="omite releer la API completa (útil en corridas largas)")
    a = ap.parse_args()
    os.environ.setdefault("SIM_MODO", "local")
    cfg = config.cargar(modo="local")
    cat = catalogo.cargar()
    R: dict = {}

    root = pymysql.connect(host="127.0.0.1", port=a.mysql_puerto, user="root", password=a.root,
                           cursorclass=pymysql.cursors.DictCursor)
    cur = root.cursor()
    cur.execute("SELECT * FROM simulador.reloj")
    reloj = cur.fetchone()
    cur.execute("SELECT valor FROM simulador.estado WHERE clave='sim_ahora'")
    sim_ahora = int(float(cur.fetchone()["valor"]))
    R["reloj"] = {"factor": reloj["factor"], "minutos_reales": round((time.time() - reloj["inicio_real"]) / 60, 1),
                  "horas_simuladas": round((sim_ahora - reloj["inicio_sim"]) / 3600, 1)}

    # ---------------------------------------------------------------- 1) PLAN vs BITÁCORA
    cur.execute("SELECT aerolinea, numero_vuelo, fecha_salida FROM simulador.nacidos")
    nacidos = cur.fetchall()
    progs = {(vp.aerolinea, vp.numero_vuelo): vp for vp in cat.programados}
    esperado_hasta_ahora, en_plan = Counter(), Counter()
    for n in nacidos:
        vi = cat.instancia(progs[(n["aerolinea"], n["numero_vuelo"])], n["fecha_salida"])
        evs = plan.planear(vi, cfg["semilla"], cat.aerolineas[vi.aerolinea].tasa_cancelacion,
                           cfg["errores"]["negocio"], cat)
        for e in evs:
            if e.inyectado:
                en_plan[e.inyectado] += 1
                if e.t <= sim_ahora:
                    esperado_hasta_ahora[e.inyectado] += 1
    cur.execute("SELECT tipo, COUNT(*) n FROM simulador.errores_inyectados GROUP BY tipo")
    bitacora = {f["tipo"]: f["n"] for f in cur.fetchall()}
    negocio = {}
    for tipo, p in cfg["errores"]["negocio"].items():
        negocio[tipo] = {"configurado_por_vuelo": p, "observado_por_vuelo": round(en_plan[tipo] / len(nacidos), 4),
                         "esperado_hasta_ahora": esperado_hasta_ahora[tipo], "en_bitacora": bitacora.get(tipo, 0)}
    R["negocio"] = {"vuelos_nacidos": len(nacidos), "por_tipo": negocio,
                    "bitacora_coincide_con_plan": all(abs(v["esperado_hasta_ahora"] - v["en_bitacora"]) <= 2
                                                      for v in negocio.values())}
    cur.execute("SELECT clave, valor FROM simulador.contadores")
    cont = {f["clave"]: int(f["valor"]) for f in cur.fetchall()}
    cur.execute("SELECT COUNT(*) n FROM simulador.sftp_archivos WHERE version=1 AND estado='escrito'")
    v1 = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(DISTINCT ventana) n FROM simulador.sftp_archivos WHERE estado='escrito'")
    ventanas = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM simulador.verdad_vuelos WHERE aerolinea='XJ' AND cerrado_sim IS NOT NULL")
    cierres_c = cur.fetchone()["n"]
    pub_a = cont.get("api.publicados", 1)
    R["bitacora_vs_config"] = {
        "api.evento_tarde": [cfg["errores"]["api"]["tarde"], round(bitacora.get("evento_tarde", 0) / pub_a, 4)],
        "api.duplicado (incluye ráfaga)": [cfg["errores"]["api"]["duplicado"], round(bitacora.get("duplicado", 0) / pub_a, 4)],
        "api.offset_local": [cfg["errores"]["api"]["offset_local"], round(cont.get("api.offset_local", 0) / pub_a, 4)],
        "api.error_5xx": [cfg["errores"]["api"]["error_5xx"],
                          round(cont.get("api.5xx_inyectado", 0) / max(1, cont.get("api.peticiones", 1)
                                                                       - cont.get("api.429", 0)), 4)],
        "sftp.truncado": [cfg["errores"]["sftp"]["truncado"], round(bitacora.get("archivo_truncado", 0) / max(1, v1), 4)],
        "sftp.reenvio_corregido": [cfg["errores"]["sftp"]["reenvio_corregido"],
                                   round(bitacora.get("reenvio_corregido", 0) / max(1, v1), 4)],
        "sftp.tarde (por ventana)": [cfg["errores"]["sftp"]["tarde"],
                                     round(bitacora.get("archivo_tarde", 0) / 2 / max(1, ventanas), 4)],
        "mysql.transaccion_larga (por lote)": [cfg["errores"]["mysql"]["transaccion_larga"],
                                               round(bitacora.get("transaccion_larga", 0) / max(1, cont.get("mysql.lotes", 1)), 4)],
        "mysql.correccion_manual (por cierre)": [cfg["errores"]["mysql"]["correccion_manual"],
                                                 round(bitacora.get("correccion_manual", 0) / max(1, cierres_c), 4)],
        "_denominadores": {"eventos_api": pub_a, "peticiones_api": cont.get("api.peticiones"), "archivos_v1": v1,
                           "ventanas_sftp": ventanas, "lotes_mysql": cont.get("mysql.lotes"), "cierres_c": cierres_c},
    }

    # ---------------------------------------------------------------- 2) API observada
    st = Counter()
    evs = [] if a.rapido else leer_api(a.api, a.llave, st)
    ids = Counter(e["id_evento"] for e in evs)
    visto_asig = set()
    invertidos = 0
    for e in evs:
        k = (e["numero_vuelo"], e["fecha_salida_local"], e.get("asiento"), e.get("pasajero") or e.get("id_pasajero"))
        if e["tipo"] == "asiento_asignado":
            visto_asig.add(k)
        elif e["tipo"] == "asiento_liberado" and k not in visto_asig:
            invertidos += 1
    rafaga = Counter()
    for _ in range(80):
        r = requests.get(f"{a.api}/v1/eventos", headers={"X-API-Key": a.llave}, params={"limite": 1}, timeout=10)
        rafaga[r.status_code] += 1
        if r.status_code == 429:
            rafaga["retry_after"] = int(r.headers["Retry-After"])
    R["api_observada"] = {
        "eventos": len(evs), "unicos": len(ids), "tasa_duplicados": round((len(evs) - len(ids)) / max(1, len(ids)), 4),
        "tasa_offset_-06:00": round(sum(e["ocurrido_en"].endswith("-06:00") for e in evs) / max(1, len(evs)), 4),
        "versiones": dict(Counter(e["version_esquema"] for e in evs)),
        "liberaciones_antes_de_su_asignacion": invertidos,
        "asientos_inexistentes": sum(1 for e in evs if e.get("asiento") and e["asiento"] not in ASIENTOS),
        "vuelos_origen_igual_destino": sum(1 for e in evs if e["tipo"] == "vuelo_creado" and e["origen"] == e["destino"]),
        "codigos_http_al_leer": dict(st), "ráfaga_80_peticiones": dict(rafaga),
    }

    # ---------------------------------------------------------------- 3) SFTP observado
    t = paramiko.Transport(("localhost", a.sftp_puerto))
    t.connect(username="alumno", password=a.alumno)
    sftp = paramiko.SFTPClient.from_transport(t)
    obs = Counter()
    columnas = Counter()
    for n in sorted(sftp.listdir("salida")):
        if not n.endswith(".csv"):
            obs["no_csv_en_listado"] += 1
            continue
        with sftp.open(f"salida/{n}", "rb") as fh:
            b = fh.read()
        obs["archivos"] += 1
        if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
            cod, texto = "utf-16", b.decode("utf-16")
        else:
            try:
                cod, texto = "utf-8", b.decode("utf-8")
            except UnicodeDecodeError:
                cod, texto = "cp1252", b.decode("cp1252")
        obs[f"codificacion_{cod}"] += 1
        enc = texto.splitlines()[0] if texto else ""
        sep = ";" if enc.count(";") > enc.count(",") else ","
        obs[f"separador_{sep}"] += 1
        filas = list(csv.reader(io.StringIO(texto), delimiter=sep))
        if filas and (not texto.endswith(("\n", "\r\n")) or any(len(f) != len(filas[0]) for f in filas[1:])):
            obs["truncado_visible_ahora"] += 1
        if "_asientos_" in n and filas:
            columnas["con_clase" if "clase" in filas[0] else "sin_clase"] += 1
    sftp.close()
    t.close()
    R["sftp_observado"] = {**obs, **{f"asientos_{k}": v for k, v in columnas.items()}}
    if a.monitor and Path(a.monitor).exists():
        tmp_vistos, cambios = set(), set()
        ultimo: dict[str, int] = {}
        for linea in open(a.monitor, encoding="utf-8"):
            reg = json.loads(linea)
            for n, tam in (reg.get("b") or {}).items():
                if n.endswith(".tmp"):
                    tmp_vistos.add(n)
                elif n in ultimo and ultimo[n] != tam:
                    cambios.add(n)
                if not n.endswith(".tmp"):
                    ultimo[n] = tam
        R["sftp_observado"]["tmp_vistos_por_el_monitor"] = len(tmp_vistos)
        R["sftp_observado"]["archivos_que_cambiaron_de_tamano"] = sorted(cambios)

    # ---------------------------------------------------------------- 4) MySQL observado (alumno)
    al = pymysql.connect(host="127.0.0.1", port=a.mysql_puerto, user="alumno", password=a.alumno,
                         database="aerolinea_c", cursorclass=pymysql.cursors.DictCursor)
    c2 = al.cursor()
    c2.execute("SELECT * FROM vuelos")
    vs = c2.fetchall()
    c2.execute("SELECT * FROM asignaciones")
    asg = c2.fetchall()
    icao = {x.icao for x in cat.aeropuertos.values()}
    red = {(cat.aeropuertos[o].icao, cat.aeropuertos[d].icao) for o, d in cat.red("XJ")}
    est = {v["id"]: v["estado"] for v in vs}
    por_asiento = Counter((x["vuelo_id"], x["asiento"]) for x in asg)
    c2.execute("SHOW VARIABLES WHERE Variable_name IN ('log_bin','binlog_format','binlog_row_image')")
    variables = {f["Variable_name"]: f["Value"] for f in c2.fetchall()}
    c2.execute("SHOW BINARY LOGS")
    logs = c2.fetchall()
    bases = Counter()
    for lg in logs[-2:]:
        c2.execute(f"SHOW BINLOG EVENTS IN '{lg['Log_name']}' LIMIT 20000")
        for ev in c2.fetchall():
            if ev["Event_type"] == "Table_map":
                m = re.search(r"\(`?(\w+)`?\.", ev["Info"])
                bases[m.group(1) if m else ev["Info"]] += 1
    R["mysql_observado"] = {
        "vuelos": len(vs), "asignaciones": len(asg),
        "codigos_icao_validos": all(v["origen"] in icao and v["destino"] in icao for v in vs),
        "origen_igual_destino": sum(v["origen"] == v["destino"] for v in vs),
        "rutas_fuera_de_red": sum((v["origen"], v["destino"]) not in red and v["origen"] != v["destino"] for v in vs),
        "asientos_con_dos_pasajeros": sum(1 for n in por_asiento.values() if n > 1),
        "asientos_inexistentes": sum(1 for x in asg if x["asiento"] not in ASIENTOS),
        "asignaciones_en_vuelos_cancelados": sum(1 for x in asg if est.get(x["vuelo_id"]) == "CANCELADO"),
        "binlog": variables, "binlog_archivos": len(logs), "tablas_en_binlog_por_base": dict(bases),
    }
    if a.monitor and Path(a.monitor).exists():
        polls = []
        for linea in open(a.monitor, encoding="utf-8"):
            reg = json.loads(linea)
            c = reg.get("c")
            if c and c["asig_max_id"]:
                polls.append((c["asig_max_id"], datetime.fromisoformat(c["asig_max_act"])))
        perdidas, traslape = set(), 0.0
        # Filas vigentes que aparecieron DESPUÉS de una lectura con marca de agua mayor que la suya.
        for x in asg:
            for mid, mact in polls:
                if x["id"] > mid and x["actualizado_en"] < mact:
                    perdidas.add(x["id"])
                    traslape = max(traslape, (mact - x["actualizado_en"]).total_seconds())
                    break
        R["mysql_observado"]["trampa_marca_de_agua"] = {
            "lecturas_del_monitor": len(polls),
            "filas_vigentes_que_perderia_una_marca_sin_traslape": len(perdidas),
            "traslape_minimo_necesario_s_simulados": traslape}
    with open(a.salida, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=2, ensure_ascii=False, default=str)
    print(json.dumps(R, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
