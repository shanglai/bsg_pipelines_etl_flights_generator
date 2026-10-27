"""Credenciales por equipo (modo servidor) a partir de config/equipos.yaml.

Por cada equipo:
- MySQL: usuario de solo lectura sobre aerolinea_c (REQUIRE SSL), con privilegios de replicación (CDC).
- SFTP: usuario enjaulado (chroot) con su carpeta salida/ de solo lectura.
- API: llave propia con su propio límite de peticiones (se guarda solo su sha256).

Es idempotente: si el equipo ya tiene credenciales en el archivo de secretos, se reutilizan.
Las credenciales en claro quedan SOLO en el archivo de secretos (fuera del repo).
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
import secrets
from pathlib import Path

import yaml

from .generador import asegurar_usuario_mysql

NOMBRE = re.compile(r"^[a-z][a-z0-9_]{2,30}$")


def aplicar(cn, archivo: Path, secretos: Path, sftp_conf: Path, uid_inicial: int = 2001) -> list[dict]:
    cfg = yaml.safe_load(Path(archivo).read_text(encoding="utf-8")) or {}
    secretos = Path(secretos)
    previos: dict[str, dict] = {}
    if secretos.exists():
        with secretos.open(encoding="utf-8") as f:
            previos = {r["equipo"]: r for r in csv.DictReader(f)}
    limite_omision = int(cfg.get("limite_por_minuto", 60))
    filas = []
    tls = os.environ.get("MYSQL_TLS", "0") == "1"
    for i, eq in enumerate(cfg.get("equipos", [])):
        nombre = eq["nombre"] if isinstance(eq, dict) else str(eq)
        if not NOMBRE.match(nombre):
            raise ValueError(f"Nombre de equipo inválido: {nombre!r} (minúsculas, dígitos y _; 3 a 31 caracteres)")
        limite = int(eq.get("limite_por_minuto", limite_omision)) if isinstance(eq, dict) else limite_omision
        prev = previos.get(nombre, {})
        fila = {
            "equipo": nombre,
            "mysql_usuario": nombre,
            "mysql_contrasena": prev.get("mysql_contrasena") or secrets.token_urlsafe(14),
            "sftp_usuario": nombre,
            "sftp_contrasena": prev.get("sftp_contrasena") or secrets.token_urlsafe(14),
            "api_llave": prev.get("api_llave") or f"ak_{secrets.token_urlsafe(24)}",
            "limite_por_minuto": limite,
            "sftp_uid": prev.get("sftp_uid") or uid_inicial + i,
        }
        asegurar_usuario_mysql(cn, nombre, fila["mysql_contrasena"], tls)
        with cn.cursor() as cur:
            cur.execute("INSERT INTO simulador.equipos (nombre, llave_api_sha256, limite_por_minuto) VALUES (%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE llave_api_sha256=VALUES(llave_api_sha256), "
                        "limite_por_minuto=VALUES(limite_por_minuto)",
                        (nombre, hashlib.sha256(fila["api_llave"].encode()).hexdigest(), limite))
        cn.commit()
        filas.append(fila)
    secretos.parent.mkdir(parents=True, exist_ok=True)
    with secretos.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()) if filas else ["equipo"])
        w.writeheader()
        w.writerows(filas)
    os.chmod(secretos, 0o600)
    sftp_conf = Path(sftp_conf)
    sftp_conf.parent.mkdir(parents=True, exist_ok=True)
    locales = [x for x in os.environ.get("SFTP_USUARIOS_CONF", "").split() if x]
    sftp_conf.write_text("\n".join(locales + [f"{r['sftp_usuario']}:{r['sftp_contrasena']}:{r['sftp_uid']}"
                                              for r in filas]) + "\n", encoding="utf-8")
    os.chmod(sftp_conf, 0o600)
    return filas
