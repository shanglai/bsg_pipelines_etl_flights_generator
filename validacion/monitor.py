"""Monitor de fondo para la validación (solo lectura, con credenciales de alumno).

Cada pocos segundos registra en JSONL:
- MySQL C: MAX(id) y MAX(actualizado_en) de asignaciones y vuelos (para medir la trampa de la marca de agua).
- SFTP B: listado de salida/ (para ver archivos .tmp y cambios de tamaño = reenvíos).

    python validacion/monitor.py --minutos 35 --salida validacion/salidas/monitor.jsonl
"""
from __future__ import annotations

import argparse
import json
import time

import paramiko
import pymysql


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutos", type=float, default=35)
    ap.add_argument("--salida", required=True)
    ap.add_argument("--mysql-puerto", type=int, default=3307)
    ap.add_argument("--sftp-puerto", type=int, default=2222)
    ap.add_argument("--contrasena", default="alumno-local-123")
    a = ap.parse_args()
    fin = time.time() + 60 * a.minutos
    t = paramiko.Transport(("localhost", a.sftp_puerto))
    t.connect(username="alumno", password=a.contrasena)
    sftp = paramiko.SFTPClient.from_transport(t)
    n = 0
    with open(a.salida, "a", encoding="utf-8") as out:
        while time.time() < fin:
            reg = {"t": time.time()}
            try:
                cn = pymysql.connect(host="127.0.0.1", port=a.mysql_puerto, user="alumno", password=a.contrasena,
                                     database="aerolinea_c")
                with cn.cursor() as cur:
                    cur.execute("SELECT MAX(id), MAX(actualizado_en) FROM asignaciones")
                    i, m = cur.fetchone()
                    cur.execute("SELECT MAX(id), MAX(actualizado_en) FROM vuelos")
                    vi, vm = cur.fetchone()
                cn.close()
                reg["c"] = {"asig_max_id": i, "asig_max_act": str(m), "vue_max_id": vi, "vue_max_act": str(vm)}
            except Exception as e:  # noqa: BLE001
                reg["c_error"] = str(e)
            if n % 2 == 0:
                try:
                    reg["b"] = {x.filename: x.st_size for x in sftp.listdir_attr("salida")}
                except Exception as e:  # noqa: BLE001
                    reg["b_error"] = str(e)
            out.write(json.dumps(reg) + "\n")
            out.flush()
            n += 1
            time.sleep(2.5)


if __name__ == "__main__":
    main()
