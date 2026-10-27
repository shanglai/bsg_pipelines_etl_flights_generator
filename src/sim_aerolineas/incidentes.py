"""Incidentes que el instructor dispara en clase (CLI o endpoint con token).

Cada incidente queda en simulador.incidentes, que forma parte de la verdad.
"""
from __future__ import annotations

import json
import time

from .reloj import Reloj

TIPOS = {
    "caida_api": "La API responde 503 a todo durante N minutos (los eventos se siguen acumulando).",
    "rafaga_duplicados": "Durante N minutos, una proporción alta de eventos de la API se publica dos veces "
                         "(parámetro proporcion, 0.3 por omisión).",
    "cambio_esquema_api": "La API pasa de v1 a v2 (pasajero -> id_pasajero, nuevo canal_venta). Durante "
                          "convivencia_min conviven ambas versiones; después todo es v2. Es permanente.",
    "sftp_codificacion": "Los siguientes N archivos SFTP (parámetro archivos, 2 por omisión) salen con otra "
                         "codificación (parámetro codificacion, utf-16 por omisión).",
    "pausa_c": "La aerolínea C deja de confirmar cambios en MySQL durante N minutos (o hasta reanudar_c).",
    "reanudar_c": "Termina la pausa de C: todo lo acumulado se confirma de golpe (ráfaga).",
}


def disparar(cn, reloj: Reloj, tipo: str, minutos: float | None = None, parametros: dict | None = None,
             origen: str = "cli", nota: str | None = None) -> int:
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de incidente desconocido: {tipo}. Opciones: {', '.join(TIPOS)}")
    ahora = time.time()
    parametros = dict(parametros or {})
    with cn.cursor() as cur:
        if tipo == "reanudar_c":
            cur.execute("UPDATE simulador.incidentes SET fin_real=%s, fin_sim=%s WHERE tipo='pausa_c' "
                        "AND (fin_real IS NULL OR fin_real > %s)", (ahora, int(reloj.sim(ahora)), ahora))
            fin = ahora
        elif tipo == "cambio_esquema_api":
            parametros.setdefault("convivencia_min", 30)
            fin = None
        elif tipo == "sftp_codificacion":
            parametros.setdefault("codificacion", "utf-16")
            parametros.setdefault("archivos", 2)
            fin = None
        elif tipo == "rafaga_duplicados":
            parametros.setdefault("proporcion", 0.3)
            fin = ahora + 60 * (minutos or 5)
        elif tipo == "caida_api":
            fin = ahora + 60 * (minutos or 5)
        else:  # pausa_c
            fin = ahora + 60 * minutos if minutos else None
        cur.execute(
            "INSERT INTO simulador.incidentes (tipo, inicio_real, fin_real, inicio_sim, fin_sim, parametros, "
            "origen, nota) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (tipo, ahora, fin, int(reloj.sim(ahora)), int(reloj.sim(fin)) if fin else None,
             json.dumps(parametros), origen, nota))
        nuevo = cur.lastrowid
    cn.commit()
    return nuevo


def terminar(cn, reloj: Reloj, id_incidente: int) -> None:
    ahora = time.time()
    with cn.cursor() as cur:
        cur.execute("UPDATE simulador.incidentes SET fin_real=%s, fin_sim=%s WHERE id=%s",
                    (ahora, int(reloj.sim(ahora)), id_incidente))
    cn.commit()


def activos(cn, ahora: float | None = None) -> list[dict]:
    ahora = time.time() if ahora is None else ahora
    with cn.cursor() as cur:
        cur.execute("SELECT * FROM simulador.incidentes WHERE inicio_real <= %s AND "
                    "(fin_real IS NULL OR fin_real > %s) ORDER BY id", (ahora, ahora))
        filas = cur.fetchall()
    for f in filas:
        f["parametros"] = json.loads(f["parametros"]) if isinstance(f["parametros"], str) else (f["parametros"] or {})
    return filas


def todos(cn) -> list[dict]:
    with cn.cursor() as cur:
        cur.execute("SELECT * FROM simulador.incidentes ORDER BY id")
        return cur.fetchall()
