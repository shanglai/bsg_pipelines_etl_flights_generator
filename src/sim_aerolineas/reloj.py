"""Reloj simulado: sim = inicio_sim + (real - inicio_real) * factor.

Todas las marcas de tiempo de los datos salen de aquí. La latencia de las fuentes,
en cambio, se mide en tiempo REAL (segundos de pared).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


def parsear_inicio(valor: str, ahora_real: float | None = None) -> float:
    if str(valor).strip().lower() == "ahora":
        t = ahora_real if ahora_real is not None else time.time()
        return float(int(t) // 60 * 60)
    return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class Reloj:
    inicio_real: float
    inicio_sim: float
    factor: float

    def sim(self, real: float | None = None) -> float:
        real = time.time() if real is None else real
        return self.inicio_sim + (real - self.inicio_real) * self.factor

    def real(self, sim: float) -> float:
        return self.inicio_real + (sim - self.inicio_sim) / self.factor

    def ahora_sim(self) -> float:
        return self.sim(time.time())

    def horas_simuladas(self, real: float | None = None) -> float:
        return (self.sim(real) - self.inicio_sim) / 3600


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dt_utc(ts: float) -> datetime:
    """datetime UTC sin zona (para columnas DATETIME)."""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
