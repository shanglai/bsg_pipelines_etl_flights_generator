"""Carga de configuración: YAML base + sobreposición por modo + variables de entorno."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(os.environ.get("SIM_RAIZ", Path(__file__).resolve().parents[2]))


def _mezclar(base: dict, encima: dict) -> dict:
    res = copy.deepcopy(base)
    for k, v in (encima or {}).items():
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            res[k] = _mezclar(res[k], v)
        else:
            res[k] = copy.deepcopy(v)
    return res


def _sin_errores(errores: dict) -> dict:
    """Pone en cero todas las frecuencias de error (modo limpio)."""
    limpio: dict[str, Any] = {"activos": False}
    for k, v in errores.items():
        if isinstance(v, dict):
            limpio[k] = {kk: 0.0 for kk in v}
    return limpio


def cargar(ruta: str | Path | None = None, modo: str | None = None) -> dict:
    """Lee la configuración efectiva.

    Variables de entorno que la ajustan (útiles en Docker Compose):
    SIM_CONFIG (ruta del YAML), SIM_MODO (local|servidor), SIM_FACTOR,
    SIM_INICIO, SIM_ERRORES (activos|limpio), SIM_FRACCION_VUELOS y SIM_CONFIG_EXTRA
    (fragmento JSON/YAML que se sobrepone a todo lo anterior).
    """
    ruta = Path(ruta or os.environ.get("SIM_CONFIG", RAIZ / "config/simulador.yaml"))
    base = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    modo = modo or os.environ.get("SIM_MODO", "local")
    cfg = _mezclar(base, base.get("modos", {}).get(modo, {}))
    cfg.pop("modos", None)
    cfg["modo"] = modo
    if os.environ.get("SIM_CONFIG_EXTRA"):
        # Fragmento JSON o YAML que se sobrepone al final, p. ej. '{"latencia": {"sftp": {"cada": 60}}}'
        cfg = _mezclar(cfg, yaml.safe_load(os.environ["SIM_CONFIG_EXTRA"]))
    if os.environ.get("SIM_FACTOR"):
        cfg["reloj"]["factor"] = float(os.environ["SIM_FACTOR"])
    if os.environ.get("SIM_INICIO"):
        cfg["reloj"]["inicio_simulado"] = os.environ["SIM_INICIO"]
    if os.environ.get("SIM_FRACCION_VUELOS"):
        cfg["volumen"]["fraccion_vuelos"] = float(os.environ["SIM_FRACCION_VUELOS"])
    if os.environ.get("SIM_ERRORES", "activos").lower() == "limpio" or not cfg["errores"].get("activos", True):
        cfg["errores"] = _sin_errores(cfg["errores"])
        # Los cambios programados (esquema v2, columna nueva) también son errores: fuera en modo limpio.
        cfg["programado"] = {"sftp_columna_nueva_desde_hora": float("inf"), "incidentes": []}
    return cfg


@lru_cache(maxsize=1)
def actual() -> dict:
    return cargar()


@dataclass(frozen=True)
class ConexionMySQL:
    host: str
    puerto: int
    usuario: str
    contrasena: str
    tls: bool

    @classmethod
    def desde_entorno(cls) -> "ConexionMySQL":
        return cls(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            puerto=int(os.environ.get("MYSQL_PUERTO", "3306")),
            usuario=os.environ.get("MYSQL_USUARIO_SIM", "root"),
            contrasena=os.environ.get("MYSQL_ROOT_PASSWORD", ""),
            tls=os.environ.get("MYSQL_TLS", "0") == "1",
        )
