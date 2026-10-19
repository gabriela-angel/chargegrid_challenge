"""Gráfico de faturamento (apresentação) e ícone de informação das Configurações."""
import main
from main import revenue_chart_view, templates


def _serie(n, unit):
    return {"period": "day", "unit": unit, "total_rs": 100.0, "total_kwh": 40.0, "sessions": 5,
            "previous_total_rs": 50.0, "delta_pct": 100.0, "compare_label": "ontem até esta hora",
            "current_index": 1,
            "buckets": [{"start": f"2026-10-14T{h:02d}:00", "revenue_rs": 10.0 * h, "kwh": float(h), "sessions": 1}
                        for h in range(n)]}


def test_view_do_grafico_tem_uma_barra_por_bucket_e_alturas_validas():
    v = revenue_chart_view(_serie(24, "hour"), "dia")
    assert len(v["bars"]) == 24
    assert all(0 <= b["height"] <= 100 for b in v["bars"])
    assert sum(1 for b in v["bars"] if b["is_current"]) == 1


def test_barras_renderizam_tooltip_visual_e_nao_title_cinza():
    html = templates.get_template("partials/revenue_chart.html").render(
        chart=revenue_chart_view(_serie(24, "hour"), "dia"), periodo="dia")
    assert html.count("data-tip ") >= 24 and " title=" not in html.split("rev-bars")[1]
    for aba in ("Dia", "Semana", "Mês"):
        assert aba in html


def test_grafico_sem_dados_mostra_aviso_em_vez_de_quebrar():
    html = templates.get_template("partials/revenue_chart.html").render(chart=None, periodo="dia")
    assert "revenue-chart" in html


def test_icone_info_gera_atributos_do_tooltip():
    tpl = templates.env.from_string(
        '{% from "partials/editable.html" import info_tip %}{{ info_tip("Tarifa-base", "Preço de referência.") }}')
    html = tpl.render()
    assert 'class="info-tip"' in html and 'data-tip-head="Tarifa-base"' in html and "Preço de referência." in html
