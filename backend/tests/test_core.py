"""
Testes do núcleo (pytest): redistribuição de potência, solar, tarifa dinâmica e
energia verde. Rodar de dentro de backend/:  python -m pytest -q
"""
from datetime import datetime, timedelta

import pytest

from core.demand_controller import DemandController
from core.solar import pv_power_kw
from core.tariff import TariffConfig, multiplier_for, price_for, validate


def make(n=12, contracted=100.0, forecast=None):
    c = DemandController("Teste", contracted)
    for i in range(1, n + 1):
        c.register_charger(f"CG-{i:02d}", 22.0, f"Vaga {i}")
    c.pv_peak_kw = 0.0           # sem solar, salvo quando o teste liga
    c.forecast_provider = forecast
    return c


# ---------------- redistribuição ----------------
def test_poucos_carros_recebem_potencia_cheia():
    c = make()
    for i in (1, 2):
        c.vehicle_connect(f"CG-0{i}", f"P{i}")
    assert [s.allocated_kw for s in c.active_sessions] == [22.0, 22.0]


def test_grid_cheio_divide_orcamento_e_nunca_ultrapassa():
    c = make()
    for i in range(1, 13):
        c.vehicle_connect(f"CG-{i:02d}", f"P{i}")
    assert all(s.allocated_kw == 7.5 for s in c.active_sessions)
    assert c.total_allocated_kw == pytest.approx(90.0)


def test_cascata_de_sobras_respeita_teto_do_hardware():
    c = make(n=5)
    c.chargers["CG-01"].max_kw = 7.0
    for i in range(1, 6):
        c.vehicle_connect(f"CG-0{i}", f"P{i}")
    kws = {s.charger_id: s.allocated_kw for s in c.active_sessions}
    assert kws["CG-01"] == 7.0
    assert c.total_allocated_kw <= c.budget_kw + 1e-6


def test_desconectar_libera_e_redistribui():
    c = make()
    for i in range(1, 13):
        c.vehicle_connect(f"CG-{i:02d}", f"P{i}")
    for i in range(1, 9):
        c.vehicle_disconnect(f"CG-{i:02d}")
    assert all(s.allocated_kw == 22.0 for s in c.active_sessions)


# ---------------- solar ----------------
def test_solar_zero_a_noite_e_pico_ao_meio_dia():
    assert pv_power_kw(2, 40) == 0.0
    assert pv_power_kw(22, 40) == 0.0
    assert pv_power_kw(12, 40) == pytest.approx(40.0)
    assert 0 < pv_power_kw(9, 40) < pv_power_kw(12, 40)


def test_solar_aumenta_orcamento_sem_pesar_na_rede():
    c = make()
    c.pv_peak_kw = 40.0
    c.sim_hour = 12.0
    for i in range(1, 13):
        c.vehicle_connect(f"CG-{i:02d}", f"P{i}")
    assert c.budget_kw == pytest.approx(130.0)
    assert c.total_allocated_kw == pytest.approx(130.0)
    assert c.grid_draw_kw == pytest.approx(90.0)
    assert c.utilization_pct == pytest.approx(90.0)  # só a rede conta no contrato


def test_energia_verde_acumula_so_com_sol():
    c = make(n=1)
    c.pv_peak_kw = 40.0
    c.vehicle_connect("CG-01", "P1")
    c.sim_hour = 12.0
    for _ in range(60):
        c.tick_energy(1.0)
    s = c.active_sessions[0]
    assert s.green_kwh == pytest.approx(s.energy_consumed_kwh)   # sol cobre tudo
    c.sim_hour = 23.0
    for _ in range(60):
        c.tick_energy(1.0)
    assert 0 < s.green_kwh < s.energy_consumed_kwh               # noite: nada novo de verde
    assert c.co2_avoided_kg == pytest.approx(s.green_kwh * 0.025)


# ---------------- tarifa dinâmica ----------------
def test_multiplicador_interpola_entre_limiares():
    cfg = TariffConfig()
    assert multiplier_for(cfg, 0) == cfg.min_multiplier
    assert multiplier_for(cfg, 500) == cfg.max_multiplier
    mid = (cfg.low_pressure_pct + cfg.high_pressure_pct) / 2
    assert multiplier_for(cfg, mid) == pytest.approx((cfg.min_multiplier + cfg.max_multiplier) / 2)


def test_faixa_de_preco_fica_na_faixa_de_mercado():
    cfg = TariffConfig()
    assert 1.80 <= price_for(cfg, 1000)["price_rs_per_kwh"] <= 2.50      # pico
    assert price_for(cfg, 0)["price_rs_per_kwh"] < 1.80                  # fora do pico


def test_validacao_rejeita_configuracao_invalida():
    assert validate(TariffConfig()) is None
    assert validate(TariffConfig(base_rs_per_kwh=0))
    assert validate(TariffConfig(min_multiplier=1.5))
    assert validate(TariffConfig(low_pressure_pct=60, high_pressure_pct=50))


def test_preco_sobe_com_a_previsao_e_cai_com_o_solar():
    c = make(forecast=lambda when: 50.0 if when.hour == 19 else 10.0)
    c.pv_peak_kw = 40.0
    base = datetime(2026, 10, 15, 0, 0)
    pico = c.pricing_at(base.replace(hour=19))["price_rs_per_kwh"]
    vale = c.pricing_at(base.replace(hour=3))["price_rs_per_kwh"]
    assert pico > vale


def test_preco_da_sessao_fica_travado():
    c = make(n=2, forecast=lambda when: 50.0)
    c.vehicle_connect("CG-01", "P1", tariff_rs_per_kwh=2.10)
    c.update_tariff_config(base_rs_per_kwh=3.0)
    assert c.chargers["CG-01"].session.tariff_rs_per_kwh == 2.10
    c.tick_energy(3600.0)
    s = c.chargers["CG-01"].session
    assert s.cost_rs == pytest.approx(s.energy_consumed_kwh * 2.10)


def test_preco_informado_e_limitado_a_faixa_vigente():
    c = make(n=1)
    c.vehicle_connect("CG-01", "P1", tariff_rs_per_kwh=0.01)
    assert c.chargers["CG-01"].session.tariff_rs_per_kwh == round(1.90 * 0.75, 2)


def test_pressao_ao_vivo_encarece_mesmo_sem_previsao():
    c = make(forecast=lambda when: 0.0)
    vazio = c.pricing_now()["price_rs_per_kwh"]
    for i in range(1, 6):
        c.vehicle_connect(f"CG-{i:02d}", f"P{i}")
    assert c.pricing_now()["price_rs_per_kwh"] > vazio


# ---------------- histórico ----------------
def test_importacao_estima_tarifa_e_verde():
    c = make(forecast=lambda when: 30.0)
    c.pv_peak_kw = 40.0
    inicio = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0) - timedelta(days=1)
    r = c.import_completed_sessions([{"charger_id": "CG-02", "vehicle_id": "X", "started_at": inicio,
                                      "duration_min": 60.0, "energy_kwh": 10.0}])
    assert r["imported"] == 1
    s = c.completed_sessions[0]
    assert s.tariff_rs_per_kwh > 0 and 0 < s.green_kwh <= 10.0


def test_connect_charger_inexistente_retorna_404():
    from fastapi.testclient import TestClient
    import main, os
    with TestClient(main.app) as c:
        h = {"X-API-Key": os.environ.get("CHARGEGRID_API_KEY", "")}
        r = c.post("/api/chargers/CG-99/connect", json={"vehicle_id": "X"}, headers=h)
        assert r.status_code == 404
