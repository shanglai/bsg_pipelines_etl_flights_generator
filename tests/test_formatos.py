from datetime import datetime

from sim_aerolineas import formatos

EV = {"evento_id": "CQ-abc", "t": 1793000000, "orden": 3, "tipo": "asiento_asignado", "aerolinea": "CQ",
      "numero_vuelo": "CQ100", "fecha_salida": "2026-11-05", "asiento": "12C", "pasajero": "p123", "datos": {}}


def test_api_v1_v2():
    v1 = formatos.evento_api(EV, 1, False, 987654)
    v2 = formatos.evento_api(EV, 2, False, 987654)
    assert v1["pasajero"] == "p123" and "id_pasajero" not in v1 and "canal_venta" not in v1
    assert v2["id_pasajero"] == "p123" and "pasajero" not in v2 and v2["canal_venta"] in ("web", "app", "agencia", "mostrador")
    assert v1["version_esquema"] == 1 and v2["version_esquema"] == 2


def test_offset_mismo_instante():
    z = formatos.instante_api(EV["t"], False)
    mx = formatos.instante_api(EV["t"], True)
    assert z.endswith("Z") and mx.endswith("-06:00")
    assert datetime.fromisoformat(z.replace("Z", "+00:00")) == datetime.fromisoformat(mx)


def test_csv_b_codificacion_y_columna_nueva(cat):
    origen = {"CQ100": "MEX"}
    filas = formatos.filas_b_asientos([EV], cat, origen, 987654, con_clase=True)
    assert filas[0]["fecha_salida"] == "05/11/2026" and filas[0]["movimiento"] == "ASIGNACION"
    b = formatos.csv_bytes(filas, formatos.COLUMNAS_B_ASIENTOS_NUEVA, ";", "cp1252")
    texto = b.decode("cp1252")
    assert texto.splitlines()[0].split(";")[6] == "clase"
    ev = {**EV, "tipo": "vuelo_creado", "numero_vuelo": "JG100",
          "datos": {"origen": "GRU", "destino": "MEX", "salida_local": "2026-11-05T10:35:00",
                    "llegada_local": "2026-11-05T15:10:00", "salida_utc": 1, "llegada_utc": 2}}
    fv = formatos.filas_b_vuelos([ev], cat, {})
    b = formatos.csv_bytes(fv, formatos.COLUMNAS_B_VUELOS, ",", "cp1252")
    assert "São Paulo".encode("cp1252") in b
    try:
        b.decode("utf-8")
        assert False, "cp1252 con acentos no debe ser utf-8 válido"
    except UnicodeDecodeError:
        pass
