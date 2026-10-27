"""Página de texto para el instructor: conteos por fuente, últimos eventos e incidentes."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def _f(ts) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def texto_estado(cn) -> str:
    ahora = time.time()
    lineas = []
    with cn.cursor() as cur:
        cur.execute("SELECT * FROM simulador.reloj WHERE id=1")
        r = cur.fetchone()
        cur.execute("SELECT clave, valor FROM simulador.estado WHERE clave IN ('latido','sim_ahora')")
        est = {f["clave"]: f["valor"] for f in cur.fetchall()}
        lineas += ["SIMULADOR DE AEROLÍNEAS - ESTADO", "=" * 60,
                   f"Hora real (UTC):        {_f(ahora)}",
                   f"Reloj simulado (UTC):   {_f(est.get('sim_ahora'))}   factor={r['factor'] if r else '-'}",
                   f"Inicio real / simulado: {_f(r['inicio_real']) if r else '-'} / {_f(r['inicio_sim']) if r else '-'}",
                   f"Último latido del generador hace {ahora - float(est.get('latido', 0)):.1f} s", ""]
        cur.execute("SELECT estado, COUNT(*) n FROM simulador.verdad_vuelos GROUP BY estado")
        vv = {f["estado"]: f["n"] for f in cur.fetchall()}
        cur.execute("SELECT COUNT(*) n FROM simulador.verdad_eventos")
        ne = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM simulador.agenda")
        na = cur.fetchone()["n"]
        lineas += ["VERDAD", "-" * 60, f"Vuelos por estado: {vv}", f"Eventos verdaderos: {ne}   en agenda (futuros): {na}", ""]
        cur.execute("SELECT fuente, COUNT(*) n, MIN(publicar_en) mn FROM simulador.salida GROUP BY fuente")
        cola = {f["fuente"]: (f["n"], f["mn"]) for f in cur.fetchall()}
        cur.execute("SELECT COUNT(*) n, MAX(seq) mx, MAX(publicado_en) ult FROM simulador.api_eventos")
        a = cur.fetchone()
        cur.execute("SELECT COUNT(*) n FROM aerolinea_c.vuelos")
        cv = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n, MAX(actualizado_en) ult FROM aerolinea_c.asignaciones")
        ca = cur.fetchone()
        cur.execute("SELECT COUNT(*) n, MAX(escrito_en) ult FROM simulador.sftp_archivos WHERE estado='escrito'")
        sb = cur.fetchone()
        cur.execute("SELECT nombre FROM simulador.sftp_archivos WHERE estado='escrito' ORDER BY escrito_en DESC LIMIT 2")
        ultimos_b = [f["nombre"] for f in cur.fetchall()]
        lineas += ["FUENTES", "-" * 60,
                   f"A (API)   eventos retenidos={a['n']} último seq={a['mx']} última publicación={_f(a['ult'])} "
                   f"en cola={cola.get('A', (0,))[0]}",
                   f"B (SFTP)  versiones escritas={sb['n']} última={_f(sb['ult'])} en cola={cola.get('B', (0,))[0]} "
                   f"últimos={ultimos_b}",
                   f"C (MySQL) vuelos={cv} asignaciones vigentes={ca['n']} último actualizado_en={ca['ult']} "
                   f"en cola={cola.get('C', (0,))[0]}", ""]
        cur.execute("SELECT * FROM simulador.incidentes WHERE fin_real IS NULL OR fin_real > %s ORDER BY id", (ahora,))
        act = cur.fetchall()
        lineas += ["INCIDENTES ACTIVOS", "-" * 60]
        lineas += [f"#{i['id']} {i['tipo']} desde {_f(i['inicio_real'])} hasta {_f(i['fin_real'])} {i['parametros']}"
                   for i in act] or ["(ninguno)"]
        cur.execute("SELECT tipo, COUNT(*) n FROM simulador.errores_inyectados GROUP BY tipo ORDER BY tipo")
        lineas += ["", "ERRORES INYECTADOS (acumulado)", "-" * 60]
        lineas += [f"{f['tipo']:<32} {f['n']}" for f in cur.fetchall()] or ["(ninguno)"]
        cur.execute("SELECT clave, valor FROM simulador.contadores ORDER BY clave")
        lineas += ["", "CONTADORES", "-" * 60]
        lineas += [f"{f['clave']:<32} {f['valor']}" for f in cur.fetchall()]
        cur.execute("SELECT aerolinea, numero_vuelo, fecha_salida, tipo, asiento, ocurrido_sim "
                    "FROM simulador.verdad_eventos ORDER BY ocurrido_sim DESC LIMIT 10")
        lineas += ["", "ÚLTIMOS 10 EVENTOS VERDADEROS", "-" * 60]
        lineas += [f"{_f(f['ocurrido_sim'])} {f['aerolinea']} {f['numero_vuelo']} {f['fecha_salida']} {f['tipo']} "
                   f"{f['asiento'] or ''}" for f in cur.fetchall()]
    return "\n".join(lineas) + "\n"
