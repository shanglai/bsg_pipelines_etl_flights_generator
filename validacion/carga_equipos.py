"""Carga de N equipos simultáneos contra el modo servidor (para medir memoria y verificar credenciales).

Cada equipo, con SUS credenciales (archivo de `sim equipos aplicar`):
- API: pagina con su llave (respeta 429/Retry-After).
- SFTP: lista y descarga los archivos nuevos cada 60 s.
- MySQL: consulta incremental por TLS cada 30 s.
Además prueba el aislamiento: la llave de un equipo no sirve sin X-API-Key válida, el usuario MySQL no ve
`simulador` y el usuario SFTP solo ve su carpeta.

    python validacion/carga_equipos.py --credenciales secretos/credenciales_equipos.csv --minutos 10
"""
from __future__ import annotations

import argparse
import csv
import json
import ssl
import threading
import time
from collections import Counter

import paramiko
import pymysql
import requests

RES: dict[str, Counter] = {}


def equipo(c: dict, host: str, fin: float) -> None:
    st = RES.setdefault(c["equipo"], Counter())
    s = requests.Session()
    s.headers["X-API-Key"] = c["api_llave"]
    cursor, vistos = None, set()
    prox_sftp = prox_mysql = 0.0
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    marca = "1970-01-01"
    while time.time() < fin:
        try:
            r = s.get(f"http://{host}:8000/v1/eventos", params={"limite": 500, **({"cursor": cursor} if cursor else {})},
                      timeout=30)
            st[f"api_{r.status_code}"] += 1
            if r.status_code == 200:
                d = r.json()
                st["api_eventos"] += len(d["datos"])
                cursor = d["siguiente_cursor"]
                if not d["hay_mas"]:
                    time.sleep(5)
            elif r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1)))
            else:
                time.sleep(1)
        except Exception as e:  # noqa: BLE001
            st[f"api_excepcion_{type(e).__name__}"] += 1
        if time.time() > prox_sftp:
            prox_sftp = time.time() + 60
            try:
                t = paramiko.Transport((host, 2222))
                t.connect(username=c["sftp_usuario"], password=c["sftp_contrasena"])
                sf = paramiko.SFTPClient.from_transport(t)
                raiz = sf.listdir(".")
                st["sftp_raiz_" + ",".join(sorted(raiz))] = 1
                for n in sf.listdir("salida"):
                    if n.endswith(".csv") and n not in vistos:
                        with sf.open(f"salida/{n}", "rb") as fh:
                            st["sftp_bytes"] += len(fh.read())
                        vistos.add(n)
                st["sftp_archivos"] = len(vistos)
                sf.close()
                t.close()
            except Exception as e:  # noqa: BLE001
                st[f"sftp_excepcion_{type(e).__name__}"] += 1
        if time.time() > prox_mysql:
            prox_mysql = time.time() + 30
            try:
                cn = pymysql.connect(host=host, port=3306, user=c["mysql_usuario"], password=c["mysql_contrasena"],
                                     database="aerolinea_c", ssl=ctx, connect_timeout=10)
                with cn.cursor() as cur:
                    cur.execute("SHOW STATUS LIKE 'Ssl_cipher'")
                    st["mysql_tls_" + (cur.fetchone()[1] or "NO")] = 1
                    cur.execute("SELECT COUNT(*), MAX(actualizado_en) FROM asignaciones "
                                "WHERE actualizado_en > %s - INTERVAL 6 HOUR", (marca,))
                    n, m = cur.fetchone()
                    st["mysql_filas_leidas"] += n
                    marca = str(m or marca)
                    try:
                        cur.execute("SELECT COUNT(*) FROM simulador.verdad_vuelos")
                        st["mysql_VE_LA_VERDAD"] += 1
                    except pymysql.err.OperationalError:
                        st["mysql_sin_acceso_a_verdad"] = 1
                cn.close()
                st["mysql_consultas"] += 1
            except Exception as e:  # noqa: BLE001
                st[f"mysql_excepcion_{type(e).__name__}"] += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credenciales", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--minutos", type=float, default=10)
    ap.add_argument("--salida")
    a = ap.parse_args()
    creds = list(csv.DictReader(open(a.credenciales, encoding="utf-8")))
    # Sin TLS debe rechazar.
    try:
        pymysql.connect(host=a.host, port=3306, user=creds[0]["mysql_usuario"], password=creds[0]["mysql_contrasena"],
                        ssl_disabled=True, connect_timeout=5)
        sin_tls = "ACEPTADA (mal)"
    except pymysql.err.OperationalError as e:
        sin_tls = f"rechazada: {e.args[0]}"
    llave_mala = requests.get(f"http://{a.host}:8000/v1/eventos", headers={"X-API-Key": "no-existe"}).status_code
    fin = time.time() + 60 * a.minutos
    hilos = [threading.Thread(target=equipo, args=(c, a.host, fin)) for c in creds]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    res = {"equipos": len(creds), "conexion_mysql_sin_tls": sin_tls, "llave_invalida_http": llave_mala,
           "por_equipo": {k: dict(v) for k, v in RES.items()}}
    txt = json.dumps(res, indent=2, ensure_ascii=False)
    if a.salida:
        open(a.salida, "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
