"""
Testes do gráfico de faturamento (billing_series), do histórico como dado de treino
(session_demand_samples) e da calibração automática da tarifa pela IA.
Rodar de dentro de backend/:  python -m pytest -q
"""
from datetime import datetime, timedelta

from tests.test_core import make


def _importar(c, itens):
    c.import_completed_sessions(itens)


def _item(cid, quando, dur=60, kwh=10.0):
    return {"charger_id": cid, "vehicle_id": f"V-{cid}-{quando:%d%H%M}", "started_at": quando,
            "duration_min": dur, "energy_kwh": kwh}


def test_serie_do_dia_tem_24_barras_e_soma_so_hoje():
    c = make()
    now = datetime(2026, 10, 14, 15, 30)
    hoje = now.replace(hour=10, minute=0)
    _importar(c, [_item("CG-01", hoje), _item("CG-02", hoje - timedelta(days=3))])
    s = c.billing_series("day", now=now)
    assert len(s["buckets"]) == 24 and s["unit"] == "hour"
    assert s["sessions"] == 1                      # a de 3 dias atrás não entra no "dia"
    assert s["current_index"] == 15
    assert s["total_rs"] == round(sum(b["revenue_rs"] for b in s["buckets"]), 2)


def test_serie_semana_e_mes_tem_7_e_30_barras():
    c = make()
    now = datetime(2026, 10, 14, 15, 30)
    _importar(c, [_item("CG-01", now - timedelta(days=d, hours=2)) for d in range(0, 20)])
    assert len(c.billing_series("week", now=now)["buckets"]) == 7
    mes = c.billing_series("month", now=now)
    assert len(mes["buckets"]) == 30 and mes["sessions"] == 20


def test_comparacao_com_periodo_anterior():
    c = make()
    now = datetime(2026, 10, 14, 15, 30)
    _importar(c, [_item("CG-01", now - timedelta(hours=2)),               # hoje
                  _item("CG-02", now - timedelta(days=1, hours=2))])       # ontem, mesma faixa
    s = c.billing_series("day", now=now)
    assert s["previous_total_rs"] > 0 and s["delta_pct"] is not None


def test_periodo_invalido_levanta_erro():
    import pytest
    with pytest.raises(ValueError):
        make().billing_series("ano")


def test_amostras_de_treino_ignoram_hoje_e_cobrem_24h():
    c = make()
    ontem = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=1)
    hoje = datetime.now().replace(hour=0, minute=5, second=0, microsecond=0)
    _importar(c, [_item("CG-01", ontem, dur=60, kwh=20.0), _item("CG-02", hoje, dur=10, kwh=2.0)])
    rows = c.session_demand_samples()
    assert len(rows) == 24                      # só o dia completo (ontem), 24 horas
    nove = [r for r in rows if r["hour"] == 9][0]
    assert abs(nove["demand_kw"] - 20.0) < 0.5   # 20 kWh em 1 h = 20 kW médios


def test_limiar_manual_desliga_a_calibracao_e_auto_religa():
    c = make(forecast=lambda when: 60.0 if 18 <= when.hour <= 21 else 10.0)
    assert c.tariff_config.pressure_auto is True
    c.update_tariff_config(low_pressure_pct=20.0, high_pressure_pct=60.0)
    assert c.tariff_config.pressure_auto is False
    assert (c.tariff_config.low_pressure_pct, c.tariff_config.high_pressure_pct) == (20.0, 60.0)
    c.update_tariff_config(pressure_auto=True)
    assert c.tariff_config.pressure_auto is True
    assert c.tariff_config.low_pressure_pct != 20.0 or c.tariff_config.high_pressure_pct != 60.0


def test_calibracao_usa_percentis_da_previsao():
    c = make(forecast=lambda when: 60.0 if 18 <= when.hour <= 21 else 10.0)
    r = c.calibrate_tariff_pressure()
    limite = c.usable_limit_kw
    assert r is not None
    assert r["low"] >= 5 and r["high"] >= r["low"] + 10
    assert abs(r["high"] - 100 * 60.0 / limite) <= 1     # P90 cai nas horas de pico (4 de 24 h = 16%)
    assert abs(c.tariff_config.low_pressure_pct - r["low"]) < 1e-9


def test_calibracao_nao_faz_nada_no_modo_manual_nem_sem_modelo():
    c = make(forecast=lambda when: 50.0)
    c.update_tariff_config(low_pressure_pct=30.0, high_pressure_pct=70.0)
    assert c.calibrate_tariff_pressure() is None
    assert c.tariff_config.low_pressure_pct == 30.0
    sem_modelo = make(forecast=lambda when: 0.0)
    assert sem_modelo.calibrate_tariff_pressure() is None
