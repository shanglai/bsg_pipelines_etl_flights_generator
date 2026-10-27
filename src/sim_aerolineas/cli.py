"""Línea de comandos del simulador: `sim <comando>`.

Ejemplos (dentro del contenedor: docker compose exec generador sim ...):
  sim generador                      bucle principal (lo arranca Docker Compose)
  sim api                            servidor de la API (lo arranca Docker Compose)
  sim estado                         página de texto de estado
  sim incidente disparar caida_api --minutos 10
  sim incidente disparar sftp_codificacion --param codificacion=utf-16 --param archivos=2
  sim incidente listar | sim incidente terminar 3
  sim verdad exportar --salida /entregas/verdad [--hasta 2026-11-05T12:00:00Z]
  sim calificar --entrega /entregas/equipo01.duckdb [--corte ...] [--margen-min 15] [--detalle dir]
  sim equipos aplicar --archivo /config/equipos.yaml
  sim bd reiniciar --confirmar       borra TODO (verdad incluida) y reinicia el reloj
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def _valor(x: str):
    try:
        return json.loads(x)
    except json.JSONDecodeError:
        return x


def _momento(x: str | None) -> int | None:
    if not x:
        return None
    return int(datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="sim", description="Simulador de aerolíneas (BSG Institute)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generador")
    a = sub.add_parser("api")
    a.add_argument("--puerto", type=int, default=int(os.environ.get("API_PUERTO_INTERNO", "8000")))
    sub.add_parser("estado")
    i = sub.add_parser("incidente")
    isub = i.add_subparsers(dest="accion", required=True)
    d = isub.add_parser("disparar")
    d.add_argument("tipo")
    d.add_argument("--minutos", type=float)
    d.add_argument("--param", action="append", default=[], help="clave=valor (se puede repetir)")
    d.add_argument("--nota")
    isub.add_parser("listar")
    isub.add_parser("tipos")
    t = isub.add_parser("terminar")
    t.add_argument("id", type=int)
    v = sub.add_parser("verdad")
    vsub = v.add_subparsers(dest="accion", required=True)
    ve = vsub.add_parser("exportar")
    ve.add_argument("--salida", required=True)
    ve.add_argument("--hasta", help="ISO UTC simulado; por omisión, ahora")
    c = sub.add_parser("calificar")
    c.add_argument("--entrega", required=True, help="archivo .duckdb o carpeta con Parquet")
    c.add_argument("--verdad", help="carpeta exportada con `sim verdad exportar` (si no, se lee de MySQL)")
    c.add_argument("--corte", help="ISO UTC simulado; por omisión, lo más reciente de la verdad")
    c.add_argument("--margen-min", type=float, default=15.0, help="minutos REALES de margen (latencia)")
    c.add_argument("--detalle", help="carpeta para CSV con las diferencias y resultado.json")
    c.add_argument("--json", action="store_true")
    e = sub.add_parser("equipos")
    esub = e.add_subparsers(dest="accion", required=True)
    ea = esub.add_parser("aplicar")
    ea.add_argument("--archivo", default="/config/equipos.yaml")
    ea.add_argument("--secretos", default="/datos/secretos/credenciales_equipos.csv")
    ea.add_argument("--sftp-conf", default="/datos/sftp_conf/users.conf")
    b = sub.add_parser("bd")
    bsub = b.add_subparsers(dest="accion", required=True)
    br = bsub.add_parser("reiniciar")
    br.add_argument("--confirmar", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "generador":
        from .generador import main as gen
        gen()
        return
    if args.cmd == "api":
        import uvicorn
        uvicorn.run("sim_aerolineas.api.app:app", host="0.0.0.0", port=args.puerto, workers=1,
                    log_level=os.environ.get("SIM_LOG", "info").lower(), access_log=False)
        return

    from . import bd, config, incidentes
    from .generador import obtener_reloj
    cn = bd.conectar(binlog=False, reintentos=3)
    cfg = config.actual()
    reloj = obtener_reloj(cn, cfg)

    if args.cmd == "estado":
        from .estado import texto_estado
        print(texto_estado(cn))
    elif args.cmd == "incidente":
        if args.accion == "tipos":
            for k, txt in incidentes.TIPOS.items():
                print(f"{k:<22} {txt}")
        elif args.accion == "listar":
            for f in incidentes.todos(cn):
                print(json.dumps(f, default=str, ensure_ascii=False))
        elif args.accion == "terminar":
            incidentes.terminar(cn, reloj, args.id)
            print(f"Incidente {args.id} terminado.")
        else:
            params = dict(p.split("=", 1) for p in args.param)
            params = {k: _valor(v) for k, v in params.items()}
            nuevo = incidentes.disparar(cn, reloj, args.tipo, args.minutos, params, origen="cli", nota=args.nota)
            print(f"Incidente #{nuevo} ({args.tipo}) disparado.")
    elif args.cmd == "verdad":
        from . import verdad
        hasta = _momento(args.hasta) or int(float(bd.leer_estado(cn, "sim_ahora", "0")))
        meta = verdad.exportar(cn, reloj, hasta, Path(args.salida))
        print(json.dumps(meta, indent=2, ensure_ascii=False))
    elif args.cmd == "calificar":
        from . import calificador
        res = calificador.correr(Path(args.entrega), cn=cn, reloj=reloj,
                                 verdad_dir=Path(args.verdad) if args.verdad else None,
                                 corte_sim=_momento(args.corte), margen_min_real=args.margen_min,
                                 detalle=Path(args.detalle) if args.detalle else None)
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else calificador.texto(res))
    elif args.cmd == "equipos":
        from . import equipos
        filas = equipos.aplicar(cn, Path(args.archivo), Path(args.secretos), Path(args.sftp_conf))
        print(f"{len(filas)} equipos aplicados. Credenciales en {args.secretos} (no lo subas a Git).")
        print("Recrea el contenedor SFTP para cargar los usuarios: docker compose ... up -d --force-recreate sftp")
    elif args.cmd == "bd":
        if not args.confirmar:
            sys.exit("Esto borra la verdad y las fuentes. Repite con --confirmar.")
        with cn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS simulador")
            cur.execute("DROP DATABASE IF EXISTS aerolinea_c")
        cn.commit()
        print("Bases borradas. Reinicia el generador (docker compose restart generador api) para empezar de cero.")


if __name__ == "__main__":
    main()
