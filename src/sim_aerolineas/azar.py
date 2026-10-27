"""Azar determinista: todo número aleatorio sale de la semilla y de una llave.

Usar una llave (por ejemplo, la llave de negocio del vuelo) en lugar de un solo
generador global hace que el resultado NO dependa del orden en que el generador
procesa las cosas ni de cuántas veces se reinicia: con la misma semilla y el
mismo reloj, el mundo simulado es idéntico.
"""
from __future__ import annotations

import hashlib
import random


def _entero(*partes: object) -> int:
    h = hashlib.sha256("|".join(str(p) for p in partes).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def rng(semilla: int, *partes: object) -> random.Random:
    """Generador propio para una llave."""
    return random.Random(_entero(semilla, *partes))


def u(semilla: int, *partes: object) -> float:
    """Uniforme [0, 1) determinista para una llave."""
    return _entero(semilla, *partes) / 2**64


def hexa(semilla: int, *partes: object, n: int = 16) -> str:
    return hashlib.sha256("|".join(str(p) for p in (semilla, *partes)).encode("utf-8")).hexdigest()[:n]
