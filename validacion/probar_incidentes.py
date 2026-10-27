"""Dispara cada incidente y verifica su efecto en la fuente y su registro en la verdad.

Usa el endpoint protegido (API) para unos y la CLI dentro del contenedor para otros, así se
prueban los dos caminos. Tarda ~12 minutos reales (espera un archivo SFTP nuevo).

    python validacion/probar_incidentes.py --salida validacion/salidas/incidentes.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from collections import Counter

import paramiko
import pymysql
import requests

API = "http://localhost:8000"
LLAVE = "llave-local-123"
TOKEN = "token-instructor-123"
RES: dict = {}


def admin(tipo: str, minutos=None, parametros=None) -> int:
    r = requests.post(f"{API}/admin/incidentes", headers={"X-Token-Instructor": TOKEN},
                      json={"tipo": tipo, "minutos": minutos, "parametros": parametros or {}}, timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def cli(*args: str) -> str:
    return subprocess.run(["docker", "compose", "exec", "-T", "generador", "sim", *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def pedir(cursor=None, limite=500):
    while True:
        r = requests.get(f"{API}/v1/eventos", headers={"X-API-Key": LLAVE},
                         params={"limite": limite, **({"cursor": cursor} if cursor else {})}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers["Retry-After"]))
            continue
        if r.status_code in (500, 502):
            time.sleep(0.5)
            continue
        return r


def ultimo_cursor() -> str:
    cursor = None
    while True:
        r = pedir(cursor)
        if r.status_code == 503:
            time.sleep(2)
            continue
        d = r.json()
        cursor = d["siguiente_cursor"]
        if not d["hay_mas"]:
            return cursor


def leer_desde(cursor: str) -> tuple[list[dict], str]:
    evs = []
    while True:
        r = pedir(cursor)
        if r.status_code == 503:
            time.sleep(2)
            continue
        d = r.json()
        evs += d["datos"]
        cursor = d["siguiente_cursor"]
        if not d["hay_mas"]:
            return evs, cursor


def prueba_api() -> None:
    # 1) caída de la API (endpoint)
    i = admin("caida_api", minutos=1)
    time.sleep(4)
    codigos = Counter(pedir().status_code for _ in range(5))
    time.sleep(62)
    despues = pedir().status_code
    RES["caida_api"] = {"id": i, "durante": dict(codigos), "despues": despues,
                        "ok": codigos.get(503, 0) == 5 and despues == 200}
    # 2) ráfaga de duplicados (endpoint)
    c0 = ultimo_cursor()
    i = admin("rafaga_duplicados", minutos=2, parametros={"proporcion": 0.5})
    time.sleep(150)
    evs, c1 = leer_desde(c0)
    ids = Counter(e["id_evento"] for e in evs)
    tasa = (len(evs) - len(ids)) / max(1, len(ids))
    RES["rafaga_duplicados"] = {"id": i, "eventos": len(evs), "unicos": len(ids), "tasa_duplicados": round(tasa, 3),
                                "ok": tasa > 0.25}
    # 3) cambio de esquema v1 -> v2 con 2 minutos de convivencia (endpoint)
    i = admin("cambio_esquema_api", parametros={"convivencia_min": 2})
    time.sleep(70)
    evs, c2 = leer_desde(c1)
    conv = Counter(e["version_esquema"] for e in evs if e["id_evento"])
    time.sleep(60)                      # termina la convivencia; se lee solo lo publicado después
    c3 = ultimo_cursor()
    time.sleep(30)
    evs, _ = leer_desde(c3)
    despues = Counter(e["version_esquema"] for e in evs)
    ejemplo = next((e for e in evs if e["tipo"] == "asiento_asignado"), None)
    RES["cambio_esquema_api"] = {"id": i, "convivencia": dict(conv), "despues": dict(despues), "ejemplo_v2": ejemplo,
                                 "ok": conv.get(1, 0) > 0 and conv.get(2, 0) > 0 and set(despues) == {2}}


def prueba_mysql() -> None:
    def maximo():
        cn = pymysql.connect(host="127.0.0.1", port=3307, user="alumno", password="alumno-local-123",
                             database="aerolinea_c")
        with cn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(id) FROM asignaciones")
            n, m = cur.fetchone()
        cn.close()
        return n, m
    salida = cli("incidente", "disparar", "pausa_c", "--nota", "prueba de validación")
    time.sleep(100)                     # más que un lote (30-90 s): debe estar congelado
    a = maximo()
    time.sleep(120)
    b = maximo()
    cli("incidente", "disparar", "reanudar_c")
    time.sleep(8)
    c = maximo()
    RES["pausa_c"] = {"cli": salida, "max_id_durante": [a[1], b[1]], "max_id_despues": c[1],
                      "filas_en_rafaga": (c[1] or 0) - (b[1] or 0), "ok": a[1] == b[1] and (c[1] or 0) > (b[1] or 0)}


def prueba_sftp() -> None:
    t = paramiko.Transport(("localhost", 2222))
    t.connect(username="alumno", password="alumno-local-123")
    sftp = paramiko.SFTPClient.from_transport(t)
    antes = set(sftp.listdir("salida"))
    salida = cli("incidente", "disparar", "sftp_codificacion", "--param", "codificacion=utf-16", "--param", "archivos=2")
    fin = time.time() + 75 * 60      # una ventana puede llegar hasta 60 min tarde
    nuevos = []
    while time.time() < fin and len(nuevos) < 2:
        time.sleep(10)
        nuevos = sorted(x for x in set(sftp.listdir("salida")) - antes if x.endswith(".csv"))
    boms = {}
    for n in nuevos[:2]:
        with sftp.open(f"salida/{n}", "rb") as fh:
            boms[n] = fh.read(2).hex()
    RES["sftp_codificacion"] = {"cli": salida, "archivos": boms,
                                "ok": len(boms) == 2 and all(b in ("fffe", "feff") for b in boms.values())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", required=True)
    a = ap.parse_args()
    hilos = [threading.Thread(target=f) for f in (prueba_api, prueba_mysql, prueba_sftp)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    cn = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="raiz-local-123",
                         cursorclass=pymysql.cursors.DictCursor)
    with cn.cursor() as cur:
        cur.execute("SELECT id, tipo, origen, inicio_real, fin_real, CAST(parametros AS CHAR) p FROM simulador.incidentes")
        RES["registrados_en_la_verdad"] = cur.fetchall()
    tipos = {f["tipo"] for f in RES["registrados_en_la_verdad"]}
    RES["todos_registrados"] = {"caida_api", "rafaga_duplicados", "cambio_esquema_api", "sftp_codificacion",
                                "pausa_c", "reanudar_c"} <= tipos
    with open(a.salida, "w", encoding="utf-8") as f:
        json.dump(RES, f, indent=2, ensure_ascii=False, default=str)
    print(json.dumps({k: v.get("ok") if isinstance(v, dict) else v for k, v in RES.items()
                      if k != "registrados_en_la_verdad"}, indent=2))


if __name__ == "__main__":
    main()
