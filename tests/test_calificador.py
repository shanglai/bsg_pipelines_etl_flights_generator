import duckdb
import pytest

from sim_aerolineas import calificador

H = 3600


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute(f"""CREATE TABLE v_vuelos AS SELECT * FROM (VALUES
        ('CQ','CQ100',DATE '2026-11-05','MEX','CUN',TIMESTAMP '2026-11-05 10:00', TIMESTAMP '2026-11-05 13:00',
          1000*{H}, 1002*{H}, 900*{H}, NULL::BIGINT, 999*{H}),
        ('CQ','CQ101',DATE '2026-11-05','CUN','MEX',TIMESTAMP '2026-11-05 15:00', TIMESTAMP '2026-11-05 17:00',
          1005*{H}, 1007*{H}, 900*{H}, 950*{H}, NULL::BIGINT),
        ('XJ','XJ200',DATE '2026-11-06','TIJ','GDL',TIMESTAMP '2026-11-06 09:00', TIMESTAMP '2026-11-06 12:00',
          1100*{H}, 1103*{H}, 980*{H}, NULL::BIGINT, NULL::BIGINT),
        ('XJ','XJ201',DATE '2026-11-06','GDL','TIJ',TIMESTAMP '2026-11-06 09:00', TIMESTAMP '2026-11-06 10:00',
          1100*{H}, 1103*{H}, 1009*{H}, NULL::BIGINT, NULL::BIGINT)
      ) t(aerolinea, numero_vuelo, fecha_salida, origen, destino, salida_local, llegada_local, salida_utc,
          llegada_utc, creado_sim, cancelado_sim, cerrado_sim)""")
    c.execute("""CREATE TABLE v_asientos_cierre AS SELECT * FROM (VALUES
        ('CQ','CQ100',DATE '2026-11-05','1A','p1', 1),
        ('CQ','CQ100',DATE '2026-11-05','1B','p2', 1),
        ('CQ','CQ100',DATE '2026-11-05','2C','p3', 1)
      ) t(aerolinea, numero_vuelo, fecha_salida, asiento, pasajero, asignado_sim)""")
    return c


def _entrega(c, vuelos, asientos):
    c.execute("CREATE OR REPLACE TABLE _ev (aerolinea VARCHAR, numero_vuelo VARCHAR, fecha_salida_local DATE, "
              "origen VARCHAR, destino VARCHAR, salida_programada_utc TIMESTAMP, estado VARCHAR)")
    c.execute("CREATE OR REPLACE TABLE _ea (aerolinea VARCHAR, numero_vuelo VARCHAR, fecha_salida_local DATE, "
              "asiento VARCHAR, pasajero VARCHAR)")
    if vuelos:
        c.executemany("INSERT INTO _ev VALUES (?,?,?,?,?,to_timestamp(?) AT TIME ZONE 'UTC',?)", vuelos)
    if asientos:
        c.executemany("INSERT INTO _ea VALUES (?,?,?,?,?)", asientos)
    c.execute(f"CREATE OR REPLACE TABLE e_vuelos AS SELECT upper(aerolinea) aerolinea, "
              f"{calificador._norm_num('numero_vuelo')} numero_vuelo, fecha_salida_local fecha_salida, origen, destino, "
              "salida_programada_utc, lower(estado) estado FROM _ev")
    c.execute(f"CREATE OR REPLACE TABLE e_asientos AS SELECT upper(aerolinea) aerolinea, "
              f"{calificador._norm_num('numero_vuelo')} numero_vuelo, fecha_salida_local fecha_salida, asiento, pasajero FROM _ea")


PERFECTA_V = [("CQ", "CQ100", "2026-11-05", "MEX", "CUN", 1000 * H, "cerrado"),
              ("CQ", "CQ101", "2026-11-05", "CUN", "MEX", 1005 * H, "cancelado"),
              ("XJ", "XJ200", "2026-11-06", "TIJ", "GDL", 1100 * H, "programado")]
PERFECTA_A = [("CQ", "CQ100", "2026-11-05", "1A", "p1"), ("CQ", "CQ100", "2026-11-05", "1B", "p2"),
              ("CQ", "CQ100", "2026-11-05", "2C", "p3")]
P = calificador.Parametros(corte_sim=1010 * H, margen_sim=2 * H, hasta_sim=1010 * H)


def test_entrega_perfecta_da_100(con):
    _entrega(con, PERFECTA_V, PERFECTA_A)
    r = calificador.calificar(con, P)
    assert r["total"]["vuelos"]["completitud"] == 1.0
    assert r["total"]["asientos"]["completitud"] == 1.0
    assert r["total"]["vuelos"]["sobrantes"] == 0
    # XJ201 se creó dentro del margen: no se exige
    assert r["por_aerolinea"]["XJ"]["vuelos"]["esperados"] == 1


def test_detecta_diferencias(con):
    v = [("CQ", "cq 0100", "2026-11-05", "MEX", "CUN", 1000 * H, "programado"),   # estado distinto (y número sucio)
         ("CQ", "CQ9100", "2026-11-05", "MEX", "MEX", 1000 * H, "programado"),    # sobrante (error inyectado)
         ("XJ", "XJ200", "2026-11-06", "TIJ", "GDL", 1100 * H, "programado")]     # CQ101 faltante
    a = [("CQ", "CQ100", "2026-11-05", "1A", "p1"),
         ("CQ", "CQ100", "2026-11-05", "1B", "pX"),     # conflicto
         ("CQ", "CQ100", "2026-11-05", "31A", "p9"),    # sobrante
         ("CQ", "CQ101", "2026-11-05", "4D", "p7")]     # asiento en vuelo cancelado -> sobrante
    _entrega(con, v, a)
    r = calificador.calificar(con, P)
    cq = r["por_aerolinea"]["CQ"]
    assert cq["vuelos"]["faltantes"] == 1
    assert cq["vuelos"]["sobrantes"] == 1
    assert cq["vuelos"]["estado_distinto"] == 1
    assert cq["vuelos"]["correctos"] == 0
    s = cq["asientos"]
    assert (s["correctos"], s["conflicto"], s["faltantes"], s["sobrantes"]) == (1, 1, 1, 2)
    assert s["completitud"] == pytest.approx(1 / 3, abs=1e-5)


def test_estado_tolerante_al_margen(con):
    # XJ200 se cierra justo antes del corte: dentro del margen se acepta el estado anterior.
    con.execute(f"UPDATE v_vuelos SET cerrado_sim = {1009 * H} WHERE numero_vuelo='XJ200'")
    _entrega(con, PERFECTA_V, PERFECTA_A)
    r = calificador.calificar(con, P)
    assert r["por_aerolinea"]["XJ"]["vuelos"]["estado_distinto"] == 0
