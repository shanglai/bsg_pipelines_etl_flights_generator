"""API REST de la aerolínea A (y endpoints de instructor).

Para alumnos:
  GET /v1/eventos?cursor=<opaco>&limite=<1..500>     encabezado X-API-Key
Para todos:
  GET /salud
Para el instructor (encabezado X-Token-Instructor o ?token=):
  GET  /admin/estado            página de texto con conteos, últimos eventos e incidentes
  GET  /admin/incidentes        lista
  POST /admin/incidentes        {"tipo": "...", "minutos": N, "parametros": {...}}
  POST /admin/incidentes/{id}/terminar
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import threading
import time
from collections import Counter
from datetime import datetime, timezone

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import bd, config, incidentes
from ..generador import obtener_reloj

app = FastAPI(title="Aerolíneas Colibrí - API de eventos (simulador del curso)", version="1.0")

_local = threading.local()
_estado = {"llaves": {}, "llaves_leidas": 0.0, "incidentes": [], "inc_leidos": 0.0, "reloj": None}
_cubetas: dict[str, list[float]] = {}           # llave -> [tokens, último_momento]
_contadores: Counter = Counter()
_ultimo_vaciado = [time.time()]
_rng = random.Random(int(config.actual()["semilla"]) + 1)
_lock = threading.Lock()


def _cn():
    cn = getattr(_local, "cn", None)
    if cn is None:
        cn = bd.conectar(binlog=False)
        cn.autocommit(True)
        _local.cn = cn
    else:
        try:
            cn.ping(reconnect=True)
        except Exception:
            cn = bd.conectar(binlog=False)
            cn.autocommit(True)
            _local.cn = cn
    return cn


def _cfg() -> dict:
    return config.actual()


def _reloj():
    # Se relee cada 30 s: el generador puede re-anclarlo al volver de un apagado.
    if _estado["reloj"] is None or time.time() - _estado.get("reloj_leido", 0) > 30:
        _estado["reloj"] = obtener_reloj(_cn(), _cfg())
        _estado["reloj_leido"] = time.time()
    return _estado["reloj"]


def _sha(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()


def _llaves() -> dict[str, dict]:
    """sha256(llave) -> {equipo, limite}. Locales desde API_LLAVES, de servidor desde simulador.equipos."""
    if time.time() - _estado["llaves_leidas"] > 30:
        lim = int(_cfg()["api"]["limite_por_minuto"])
        llaves = {}
        for par in os.environ.get("API_LLAVES", "").split(","):
            if ":" in par:
                eq, ll = par.split(":", 1)
                llaves[_sha(ll.strip())] = {"equipo": eq.strip(), "limite": lim}
        try:
            with _cn().cursor() as cur:
                cur.execute("SELECT nombre, llave_api_sha256, limite_por_minuto FROM simulador.equipos")
                for f in cur.fetchall():
                    llaves[f["llave_api_sha256"]] = {"equipo": f["nombre"], "limite": int(f["limite_por_minuto"])}
        except Exception:
            pass
        _estado["llaves"] = llaves
        _estado["llaves_leidas"] = time.time()
    return _estado["llaves"]


def _incidentes() -> list[dict]:
    if time.time() - _estado["inc_leidos"] > 2:
        _estado["incidentes"] = incidentes.activos(_cn())
        _estado["inc_leidos"] = time.time()
    return _estado["incidentes"]


def _vaciar_contadores() -> None:
    if time.time() - _ultimo_vaciado[0] > 5:
        with _lock:
            datos = dict(_contadores)
            _contadores.clear()
            _ultimo_vaciado[0] = time.time()
        try:
            bd.sumar_contadores(_cn(), datos)
        except Exception:
            pass


def _token_ok(token: str | None) -> bool:
    esperado = os.environ.get("TOKEN_INSTRUCTOR", "")
    return bool(esperado) and token == esperado


def _limitar(llave_sha: str, limite: int) -> float:
    """Cubeta de fichas: `limite` por minuto. Regresa 0 si pasa o los segundos a esperar."""
    with _lock:
        ahora = time.time()
        fichas, antes = _cubetas.get(llave_sha, [float(limite), ahora])
        fichas = min(float(limite), fichas + (ahora - antes) * limite / 60)
        if fichas >= 1:
            _cubetas[llave_sha] = [fichas - 1, ahora]
            return 0.0
        _cubetas[llave_sha] = [fichas, ahora]
        return (1 - fichas) * 60 / limite


def _cursor_a_seq(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        s = base64.urlsafe_b64decode(cursor.encode() + b"==").decode()
        assert s.startswith("s:")
        return int(s[2:])
    except Exception:
        raise HTTPException(400, detail={"error": "cursor_invalido", "mensaje": "El cursor no es válido."})


def _seq_a_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"s:{seq}".encode()).decode().rstrip("=")


@app.get("/salud")
def salud():
    try:
        cn = _cn()
        _vaciar_contadores()
        latido = float(bd.leer_estado(cn, "latido", "0"))
        sim = float(bd.leer_estado(cn, "sim_ahora", "0"))
        retraso = time.time() - latido
        inc = [i["tipo"] for i in _incidentes()]
        ok = retraso < 30
        return JSONResponse(status_code=200 if ok else 503, content={
            "estado": "ok" if ok else "degradado",
            "generador_ultimo_latido_s": round(retraso, 1),
            "reloj_simulado": datetime.fromtimestamp(sim, tz=timezone.utc).isoformat(),
            "factor_reloj": _reloj().factor if _reloj() else None,
            "incidentes_activos": inc,
        })
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"estado": "error", "detalle": str(e)})


@app.get("/v1/eventos")
def eventos(request: Request, cursor: str | None = Query(None), limite: int | None = Query(None),
            x_api_key: str | None = Header(None)):
    cfg = _cfg()
    _vaciar_contadores()
    if not x_api_key or _sha(x_api_key) not in _llaves():
        raise HTTPException(401, detail={"error": "llave_invalida", "mensaje": "Falta X-API-Key o no es válida."})
    info = _llaves()[_sha(x_api_key)]
    espera = _limitar(_sha(x_api_key), info["limite"])
    with _lock:
        _contadores["api.peticiones"] += 1
    if espera > 0:
        with _lock:
            _contadores["api.429"] += 1
        return JSONResponse(status_code=429, headers={"Retry-After": str(max(1, math.ceil(espera)))},
                            content={"error": "demasiadas_peticiones",
                                     "mensaje": f"Límite de {info['limite']} peticiones por minuto por llave."})
    if any(i["tipo"] == "caida_api" for i in _incidentes()):
        with _lock:
            _contadores["api.caida_503"] += 1
        return JSONResponse(status_code=503, headers={"Retry-After": "30"},
                            content={"error": "servicio_no_disponible", "mensaje": "Mantenimiento no programado."})
    if _rng.random() < float(cfg["errores"].get("api", {}).get("error_5xx", 0)):
        codigo = _rng.choice([500, 502, 503])
        with _lock:
            _contadores[f"api.{codigo}"] += 1
            _contadores["api.5xx_inyectado"] += 1
        return JSONResponse(status_code=codigo, content={"error": "error_interno", "mensaje": "Error inesperado."})

    limite = min(int(limite or cfg["api"]["pagina_omision"]), int(cfg["api"]["pagina_max"]))
    limite = max(1, limite)
    desde = _cursor_a_seq(cursor)
    with _cn().cursor() as cur:
        cur.execute("SELECT MIN(seq) AS mn, MAX(seq) AS mx FROM simulador.api_eventos")
        rango = cur.fetchone()
        if cursor and rango["mn"] is not None and desde < rango["mn"] - 1:
            raise HTTPException(410, detail={
                "error": "cursor_expirado",
                "mensaje": f"El cursor apunta a eventos con más de {cfg['retencion']['api_horas']} h; ya no se retienen."})
        cur.execute("SELECT seq, payload FROM simulador.api_eventos WHERE seq > %s ORDER BY seq LIMIT %s",
                    (desde, limite + 1))
        filas = cur.fetchall()
    hay_mas = len(filas) > limite
    filas = filas[:limite]
    siguiente = filas[-1]["seq"] if filas else max(desde, 0)
    cuerpo = '{"datos":[' + ",".join(f["payload"] for f in filas) + '],' \
             f'"siguiente_cursor":"{_seq_a_cursor(siguiente)}","hay_mas":{"true" if hay_mas else "false"}}}'
    return Response(content=cuerpo, media_type="application/json; charset=utf-8")


# ------------------------------------------------------------------ instructor
def _requiere_token(x_token_instructor: str | None, token: str | None) -> None:
    if not _token_ok(x_token_instructor or token):
        raise HTTPException(403, detail="Se requiere el token de instructor.")


@app.get("/admin/incidentes")
def listar_incidentes(x_token_instructor: str | None = Header(None), token: str | None = None):
    _requiere_token(x_token_instructor, token)
    return json.loads(json.dumps(incidentes.todos(_cn()), default=str))


@app.post("/admin/incidentes")
def crear_incidente(cuerpo: dict = Body(...), x_token_instructor: str | None = Header(None), token: str | None = None):
    _requiere_token(x_token_instructor, token)
    try:
        nuevo = incidentes.disparar(_cn(), _reloj(), cuerpo["tipo"], cuerpo.get("minutos"),
                                    cuerpo.get("parametros"), origen="api", nota=cuerpo.get("nota"))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, detail=str(e))
    _estado["inc_leidos"] = 0
    return {"id": nuevo, "tipo": cuerpo["tipo"]}


@app.post("/admin/incidentes/{id_incidente}/terminar")
def terminar_incidente(id_incidente: int, x_token_instructor: str | None = Header(None), token: str | None = None):
    _requiere_token(x_token_instructor, token)
    incidentes.terminar(_cn(), _reloj(), id_incidente)
    _estado["inc_leidos"] = 0
    return {"id": id_incidente, "terminado": True}


@app.get("/admin/estado", response_class=PlainTextResponse)
def estado(x_token_instructor: str | None = Header(None), token: str | None = None):
    _requiere_token(x_token_instructor, token)
    from ..estado import texto_estado
    return texto_estado(_cn())
