"""Cómo se ve cada evento en cada fuente. Funciones puras (sin E/S) para probarlas fácil."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from .azar import u
from .catalogo import Catalogo

ESTADOS_B = {"vuelo_creado": "PROGRAMADO", "vuelo_cancelado": "CANCELADO", "vuelo_cerrado": "CERRADO"}
ESTADOS_C = {"vuelo_creado": "ABIERTO", "vuelo_cancelado": "CANCELADO", "vuelo_cerrado": "CERRADO"}
TIPOS_VUELO = set(ESTADOS_B)
OFFSET_MX = timezone(timedelta(hours=-6))

COLUMNAS_B_VUELOS = ["numero_vuelo", "fecha_salida", "hora_salida", "origen", "ciudad_origen", "destino",
                     "ciudad_destino", "fecha_llegada", "hora_llegada", "estado", "actualizado"]
COLUMNAS_B_ASIENTOS = ["id_movimiento", "numero_vuelo", "fecha_salida", "asiento", "pasajero", "movimiento",
                       "fecha_hora_movimiento"]
COLUMNAS_B_ASIENTOS_NUEVA = ["id_movimiento", "numero_vuelo", "fecha_salida", "asiento", "pasajero",
                             "movimiento", "clase", "fecha_hora_movimiento"]


# ---------------------------------------------------------------- API (aerolínea A)
def instante_api(t: int, offset_local: bool) -> str:
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    if offset_local:
        return dt.astimezone(OFFSET_MX).isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_utc(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evento_api(ev: dict, version: int, offset_local: bool, semilla: int) -> dict:
    """Payload publicado por la API.

    v1 -> v2: `pasajero` se renombra a `id_pasajero` y aparece `canal_venta`
    en las asignaciones.
    """
    p = {
        "id_evento": ev["evento_id"],
        "version_esquema": version,
        "tipo": ev["tipo"],
        "ocurrido_en": instante_api(ev["t"], offset_local),
        "aerolinea": ev["aerolinea"],
        "numero_vuelo": ev["numero_vuelo"],
        "fecha_salida_local": ev["fecha_salida"],
    }
    if ev["tipo"] == "vuelo_creado":
        d = ev["datos"]
        p.update({
            "origen": d["origen"], "destino": d["destino"],
            "salida_programada_local": d["salida_local"], "salida_programada_utc": _iso_utc(d["salida_utc"]),
            "llegada_programada_local": d["llegada_local"], "llegada_programada_utc": _iso_utc(d["llegada_utc"]),
        })
    elif ev["tipo"] in ("asiento_asignado", "asiento_liberado"):
        p["asiento"] = ev["asiento"]
        if version == 1:
            p["pasajero"] = ev["pasajero"]
        else:
            p["id_pasajero"] = ev["pasajero"]
            if ev["tipo"] == "asiento_asignado":
                x = u(semilla, "canal", ev["evento_id"])
                p["canal_venta"] = "web" if x < 0.45 else "app" if x < 0.75 else "agencia" if x < 0.92 else "mostrador"
    return p


# ---------------------------------------------------------------- SFTP (aerolínea B)
def _local(t: int, tz) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(tz).replace(tzinfo=None)


def _ddmmaaaa(d: str) -> str:
    a, m, dd = d[:10].split("-")
    return f"{dd}/{m}/{a}"


def origen_de(ev: dict, cat: Catalogo, origen_por_numero: dict[str, str]) -> str:
    return ev.get("datos", {}).get("origen") or origen_por_numero[ev["numero_vuelo"]]


def filas_b_vuelos(eventos: list[dict], cat: Catalogo, origen_por_numero: dict[str, str]) -> list[dict]:
    """Foto de los vuelos que cambiaron en la ventana: el último estado de cada uno."""
    ultimo: dict[tuple, dict] = {}
    for ev in sorted((e for e in eventos if e["tipo"] in TIPOS_VUELO), key=lambda e: (e["t"], e["orden"])):
        ultimo[(ev["numero_vuelo"], ev["fecha_salida"])] = ev
    filas = []
    for ev in ultimo.values():
        d = ev["datos"]
        ao, ad = cat.aeropuertos[d["origen"]], cat.aeropuertos[d["destino"]]
        sl, ll = d["salida_local"], d["llegada_local"]
        filas.append({
            "numero_vuelo": ev["numero_vuelo"],
            "fecha_salida": _ddmmaaaa(sl),
            "hora_salida": sl[11:16],
            "origen": d["origen"], "ciudad_origen": ao.ciudad,
            "destino": d["destino"], "ciudad_destino": ad.ciudad,
            "fecha_llegada": _ddmmaaaa(ll),
            "hora_llegada": ll[11:16],
            "estado": ESTADOS_B[ev["tipo"]],
            "actualizado": _local(ev["t"], ao.tz).strftime("%d/%m/%Y %H:%M:%S"),
        })
    return filas


def filas_b_asientos(eventos: list[dict], cat: Catalogo, origen_por_numero: dict[str, str],
                     semilla: int, con_clase: bool) -> list[dict]:
    filas = []
    for ev in sorted((e for e in eventos if e["tipo"] not in TIPOS_VUELO), key=lambda e: (e["t"], e["orden"])):
        tz = cat.aeropuertos[origen_de(ev, cat, origen_por_numero)].tz
        f = {
            "id_movimiento": ev["evento_id"],
            "numero_vuelo": ev["numero_vuelo"],
            "fecha_salida": _ddmmaaaa(ev["fecha_salida"]),
            "asiento": ev["asiento"],
            "pasajero": ev["pasajero"],
            "movimiento": "ASIGNACION" if ev["tipo"] == "asiento_asignado" else "LIBERACION",
            "fecha_hora_movimiento": _local(ev["t"], tz).strftime("%d/%m/%Y %H:%M:%S"),
        }
        if con_clase:
            x = u(semilla, "clase", ev["evento_id"])
            f["clase"] = "Y" if x < 0.8 else "W" if x < 0.92 else "J"
        filas.append(f)
    return filas


def csv_bytes(filas: list[dict], columnas: list[str], separador: str, codificacion: str) -> bytes:
    buf = io.StringIO()
    fin = "\r\n" if codificacion != "utf-8" else "\n"
    w = csv.DictWriter(buf, fieldnames=columnas, delimiter=separador, lineterminator=fin,
                       quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
    w.writeheader()
    w.writerows(filas)
    return buf.getvalue().encode(codificacion)
