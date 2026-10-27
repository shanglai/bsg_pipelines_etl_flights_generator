from datetime import date

import pytest

from sim_aerolineas import catalogo, config, plan


@pytest.fixture(scope="session")
def cat():
    return catalogo.cargar()


@pytest.fixture(scope="session")
def cfg():
    return config.cargar(modo="local")


@pytest.fixture(scope="session")
def muestra(cat, cfg):
    """Planes de 6 días completos de operación (≈1,500 vuelos)."""
    res = []
    for d in range(6):
        for vi in cat.vuelos_del_dia(date(2026, 11, 10 + d)):
            a = cat.aerolineas[vi.aerolinea]
            res.append((vi, plan.planear(vi, cfg["semilla"], a.tasa_cancelacion, cfg["errores"]["negocio"], cat)))
    return res
