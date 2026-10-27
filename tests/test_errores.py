import math
from collections import Counter
from datetime import date

from sim_aerolineas import config, plan
from sim_aerolineas.catalogo import ASIENTOS


def _dentro(observado: int, n: int, p: float) -> bool:
    """Intervalo de ±4 desviaciones estándar de una binomial."""
    media, de = n * p, math.sqrt(n * p * (1 - p))
    return media - 4 * de - 1 <= observado <= media + 4 * de + 1


def test_frecuencias_de_errores_de_negocio(muestra, cfg):
    n = len(muestra)
    c = Counter(e.inyectado for _, evs in muestra for e in evs if e.inyectado)
    for tipo, p in cfg["errores"]["negocio"].items():
        if tipo == "asiento_doble":
            continue  # requiere asientos ocupados; se revisa abajo
        assert _dentro(c[tipo], n, p), (tipo, c[tipo], n * p)
    assert c["asiento_doble"] > 0


def test_forma_de_cada_error(muestra, cat):
    for vi, evs in muestra:
        fin = next(e for e in reversed(evs) if e.inyectado is None and e.tipo.startswith("vuelo_") and e.tipo != "vuelo_creado")
        for e in evs:
            if e.inyectado == "asiento_inexistente":
                assert e.asiento not in ASIENTOS
            elif e.inyectado == "asignacion_vuelo_no_abierto":
                assert e.t > fin.t
            elif e.inyectado == "origen_igual_destino":
                assert e.datos["origen"] == e.datos["destino"] and e.numero_vuelo != vi.numero_vuelo
            elif e.inyectado == "ruta_fuera_de_red":
                assert e.datos["destino"] not in cat.aerolineas[vi.aerolinea].aeropuertos
            elif e.inyectado == "asiento_doble":
                previos = [x for x in evs if x.inyectado is None and x.asiento == e.asiento and x.t < e.t]
                assert previos and previos[-1].tipo == "asiento_asignado" and previos[-1].pasajero != e.pasajero


def test_modo_limpio_sin_errores(cat, monkeypatch):
    monkeypatch.setenv("SIM_ERRORES", "limpio")
    limpio = config.cargar(modo="local")
    assert limpio["errores"]["activos"] is False
    assert all(v == 0 for g in limpio["errores"].values() if isinstance(g, dict) for v in g.values())
    assert limpio["programado"]["incidentes"] == []
    for vi in cat.vuelos_del_dia(date(2026, 11, 20)):
        evs = plan.planear(vi, limpio["semilla"], 0.03, limpio["errores"]["negocio"], cat)
        assert not any(e.inyectado for e in evs)
