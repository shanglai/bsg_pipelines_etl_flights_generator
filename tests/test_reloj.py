from sim_aerolineas.reloj import Reloj, parsear_inicio


def test_reloj_ida_y_vuelta():
    r = Reloj(inicio_real=1000.0, inicio_sim=parsear_inicio("2026-11-02T00:00:00Z"), factor=60)
    assert r.sim(1060) - r.inicio_sim == 3600
    assert abs(r.real(r.sim(1234.5)) - 1234.5) < 1e-6
    assert r.horas_simuladas(1000 + 60) == 1


def test_config_extra_por_entorno(monkeypatch):
    from sim_aerolineas import config
    monkeypatch.setenv("SIM_CONFIG_EXTRA", '{"latencia": {"sftp": {"cada": 60}}}')
    cfg = config.cargar(modo="local")
    assert cfg["latencia"]["sftp"]["cada"] == 60
    assert cfg["latencia"]["api"]["min"] == 2      # lo demás no cambia
