from collections import Counter, defaultdict

from sim_aerolineas import plan
from sim_aerolineas.catalogo import ASIENTOS


def test_plan_determinista(cat, cfg):
    from datetime import date
    vi = cat.vuelos_del_dia(date(2026, 12, 1))[17]
    a = cat.aerolineas[vi.aerolinea]
    p1 = plan.planear(vi, cfg["semilla"], a.tasa_cancelacion, cfg["errores"]["negocio"], cat)
    p2 = plan.planear(vi, cfg["semilla"], a.tasa_cancelacion, cfg["errores"]["negocio"], cat)
    assert [e.__dict__ for e in p1] == [e.__dict__ for e in p2]
    p3 = plan.planear(vi, 123, a.tasa_cancelacion, cfg["errores"]["negocio"], cat)
    assert [e.evento_id for e in p1] != [e.evento_id for e in p3]


def test_reglas_de_negocio_en_la_verdad(muestra):
    for vi, evs in muestra:
        verdad = [e for e in evs if e.inyectado is None]
        assert verdad[0].tipo == "vuelo_creado"
        creado = verdad[0].t
        assert 86400 <= vi.salida_utc - creado <= 14 * 86400
        fin = verdad[-1]
        assert fin.tipo in ("vuelo_cerrado", "vuelo_cancelado")
        if fin.tipo == "vuelo_cerrado":
            assert fin.t == vi.salida_utc - 3600
        ocupados: dict[str, str] = {}
        tiempos = [e.t for e in verdad]
        assert tiempos == sorted(tiempos) and len(set(tiempos)) == len(tiempos)
        for e in verdad[1:-1]:
            assert creado < e.t < fin.t
            assert e.asiento in ASIENTOS
            if e.tipo == "asiento_asignado":
                assert e.asiento not in ocupados          # nunca dos pasajeros en un asiento
                assert e.pasajero not in ocupados.values()  # un pasajero, un asiento a la vez
                ocupados[e.asiento] = e.pasajero
            else:
                assert ocupados.pop(e.asiento) == e.pasajero  # solo se libera lo asignado
        if fin.tipo == "vuelo_cerrado":
            occ = len(ocupados) / 180
            assert 0.50 <= occ <= 0.96, (vi.llave, occ)


def test_tasa_de_cancelacion(muestra):
    por = defaultdict(Counter)
    for vi, evs in muestra:
        por[vi.aerolinea][evs[-1].tipo if evs[-1].inyectado is None else
                          next(e.tipo for e in reversed(evs) if e.inyectado is None)] += 1
    for a, c in por.items():
        tasa = c["vuelo_cancelado"] / sum(c.values())
        assert 0.005 <= tasa <= 0.07, (a, tasa)
    total = sum(c["vuelo_cancelado"] for c in por.values()) / sum(sum(c.values()) for c in por.values())
    assert 0.015 <= total <= 0.045


def test_ocupacion_promedio_realista(muestra):
    occs = []
    for vi, evs in muestra:
        if any(e.tipo == "vuelo_cerrado" for e in evs):
            ocupados = set()
            for e in evs:
                if e.inyectado:
                    continue
                if e.tipo == "asiento_asignado":
                    ocupados.add(e.asiento)
                elif e.tipo == "asiento_liberado":
                    ocupados.discard(e.asiento)
            occs.append(len(ocupados) / 180)
    assert 0.6 < sum(occs) / len(occs) < 0.9
