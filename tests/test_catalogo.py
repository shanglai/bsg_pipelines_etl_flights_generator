import itertools
from collections import Counter
from zoneinfo import ZoneInfo


def test_cuarenta_aeropuertos_reales(cat):
    aps = cat.aeropuertos.values()
    assert len(cat.aeropuertos) == 40
    paises = Counter("MX" if a.pais == "México" else "US" if a.pais == "Estados Unidos" else "LATAM" for a in aps)
    assert paises == {"MX": 22, "US": 10, "LATAM": 8}
    for a in aps:
        assert len(a.iata) == 3 and len(a.icao) == 4
        ZoneInfo(a.zona_horaria)
        assert -60 < a.latitud < 60 and -130 < a.longitud < -40
    assert len({a.icao for a in aps}) == 40


def test_redes_y_traslapes(cat):
    redes = {c: set(a.aeropuertos) for c, a in cat.aerolineas.items()}
    for c, red in redes.items():
        assert 16 <= len(red) <= 26, c
        assert len(red) < 40
        # las rutas programadas solo usan aeropuertos de la red
        assert {x for par in cat.red(c) for x in par} <= red
    for a, b in itertools.combinations(redes, 2):
        assert 6 <= len(redes[a] & redes[b]) <= 10, (a, b)
    assert set().union(*redes.values()) == set(cat.aeropuertos)


def test_horas_locales_y_utc(cat):
    from datetime import date
    vi = next(v for v in cat.vuelos_del_dia(date(2026, 11, 10)) if v.origen == "MEX" and v.destino == "CUN")
    # MEX es UTC-6 todo el año desde 2022
    assert vi.salida_utc % 86400 // 3600 == (vi.salida_local.hour + 6) % 24
    assert vi.llegada_utc > vi.salida_utc
