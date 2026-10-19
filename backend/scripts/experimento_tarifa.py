"""
ChargeGrid Intelligence — Experimento: COM x SEM tarifa dinâmica
------------------------------------------------------------------
Pergunta: a tarifa dinâmica (demand response) reduz o pico de demanda do site sem
derrubar a receita? Rodar de dentro de backend/:

    python -m scripts.experimento_tarifa            # tabelas + gráficos (se houver matplotlib)
    python -m scripts.experimento_tarifa --dias 60 --repeticoes 50 --semente 7

O QUE É REAL e o QUE É PREMISSA (importante para a apresentação):
  REAL     preço = DemandController.pricing_at() (o mesmo código do sistema), previsão de
           demanda = o modelo de IA treinado (ai/model.joblib), solar = core/solar.py,
           limite utilizável = 90 kW (100 kW contratados × 90%).
  PREMISSA a demanda de cada dia é a previsão da IA × ruído log-normal diário (σ=15%) × ruído
           por hora (σ=8%); e o COMPORTAMENTO do motorista: uma fração "flexível" dos
           motoristas aceita mudar o horário da recarga (±3 h) quando o preço de outra hora é
           mais barato; a resposta cresce com a economia e satura em 20% de desconto.
           Não há dado de campo sobre essa elasticidade — por isso o script varre a fração
           flexível (0–40%) e mostra a sensibilidade, em vez de apostar num número só.
  BASE     "sem tarifa dinâmica" = preço fixo igual à tarifa-base (R$ 1,90/kWh).
  Cenários: "normal" (demanda da IA) e "congestionado" (demanda × 1,8: um site mais cheio,
           em que o limite da rede passa a apertar).
"""
import argparse
import json
import random
import statistics as st
from datetime import datetime, timedelta
from pathlib import Path

from ai.predictor import model_metrics, predict_hour
from core.demand_controller import DemandController

CONTRATADO_KW = 100.0
SHIFT_WINDOW_H = 3          # quanto o motorista flexível aceita adiantar/atrasar
SATURATION_DISCOUNT = 0.20  # com 20% de desconto relativo, todo o flexível se desloca
DAY_SIGMA, HOUR_SIGMA = 0.15, 0.08
SCENARIOS = {"normal": 1.0, "congestionado": 1.8}
FLEX_LEVELS = [0.0, 0.10, 0.20, 0.30, 0.40]
OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "experimento"


def build_controller() -> DemandController:
    c = DemandController("Experimento", CONTRATADO_KW)
    c.forecast_provider = lambda when: predict_hour(when.hour, when.weekday())
    return c


_PRICE_CACHE: dict = {}


def day_prices(c: DemandController, day) -> list[float]:
    """Preço dinâmico de cada hora do dia (mesmo cálculo do sistema, meio da hora)."""
    if day not in _PRICE_CACHE:
        _PRICE_CACHE[day] = _day_prices(c, day)
    return _PRICE_CACHE[day]


def _day_prices(c: DemandController, day) -> list[float]:
    return [c.pricing_at(datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=30))
            ["price_rs_per_kwh"] for h in range(24)]


def shift_energy(energy: list[float], prices: list[float], flex: float) -> list[float]:
    """
    Desloca a parte flexível da energia de cada hora para horas mais baratas na janela
    de ±SHIFT_WINDOW_H. A energia que sai se reparte entre as horas mais baratas na
    PROPORÇÃO da economia de cada uma (quem economiza mais atrai mais) — não todo mundo
    na mesma hora, o que seria um exagero. O total de kWh se conserva.
    """
    out = list(energy)
    if flex <= 0:
        return out
    for h, e in enumerate(energy):
        window = range(max(0, h - SHIFT_WINDOW_H), min(23, h + SHIFT_WINDOW_H) + 1)
        gains = {k: prices[h] - prices[k] for k in window if prices[k] < prices[h]}
        if not gains:
            continue
        best = max(gains.values()) / prices[h]
        moved = e * flex * min(1.0, best / SATURATION_DISCOUNT)
        total = sum(gains.values())
        out[h] -= moved
        for k, g in gains.items():
            out[k] += moved * g / total
    return out


def simulate(c, scale: float, flex: float, days: int, seed: int, start) -> dict:
    """Uma rodada de `days` dias. Devolve métricas e a curva média do dia (kW por hora)."""
    rng = random.Random(seed)
    usable = c.usable_limit_kw
    peaks, over_kwh, hours_hi, rev, kwh_tot, avg_curve = [], 0.0, 0, 0.0, 0.0, [0.0] * 24
    for d in range(days):
        day = start + timedelta(days=d)
        prices = day_prices(c, day)
        day_noise = rng.lognormvariate(0, DAY_SIGMA)
        energy = [max(0.0, predict_hour(h, day.weekday()) * scale * day_noise * rng.lognormvariate(0, HOUR_SIGMA))
                  for h in range(24)]                       # kWh na hora = kW médio × 1 h
        energy = shift_energy(energy, prices, flex)
        grid = [max(0.0, energy[h] - c.pv_kw_at(datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=30)))
                for h in range(24)]
        peaks.append(max(grid))
        over_kwh += sum(max(0.0, g - usable) for g in grid)
        hours_hi += sum(1 for g in grid if g > 0.8 * usable)
        paid = prices if flex is not None and flex >= 0 and DYNAMIC[0] else [c.tariff_config.base_rs_per_kwh] * 24
        rev += sum(e * p for e, p in zip(energy, paid))
        kwh_tot += sum(energy)
        avg_curve = [a + g / days for a, g in zip(avg_curve, grid)]
    return {"peak_kw": st.mean(peaks), "over_limit_kwh_day": over_kwh / days,
            "hours_above_80pct_day": hours_hi / days, "revenue_day": rev / days,
            "kwh_day": kwh_tot / days, "avg_price": rev / kwh_tot, "curve": avg_curve}


DYNAMIC = [True]   # liga/desliga a cobrança dinâmica na rodada corrente


def run(days: int, reps: int, seed: int) -> dict:
    c = build_controller()
    start = datetime.now().date().replace(day=1) - timedelta(days=days)   # intervalo fixo e passado
    results = {}
    for name, scale in SCENARIOS.items():
        base_runs = []
        DYNAMIC[0] = False
        for r in range(reps):
            base_runs.append(simulate(c, scale, 0.0, days, seed + r, start))
        rows = [("fixo", 0.0, base_runs)]
        DYNAMIC[0] = True
        for flex in FLEX_LEVELS:
            rows.append(("dinamica", flex, [simulate(c, scale, flex, days, seed + r, start) for r in range(reps)]))
        results[name] = []
        b = {k: st.mean(x[k] for x in base_runs) for k in base_runs[0] if k != "curve"}
        for label, flex, runs in rows:
            m = {k: st.mean(x[k] for x in runs) for k in runs[0] if k != "curve"}
            m["peak_sd"] = st.pstdev([x["peak_kw"] for x in runs])
            m["curve"] = [st.mean(x["curve"][h] for x in runs) for h in range(24)]
            m.update({"tarifa": label, "flex": flex,
                      "peak_reduction_pct": 100 * (b["peak_kw"] - m["peak_kw"]) / b["peak_kw"],
                      "over_limit_reduction_pct": (100 * (b["over_limit_kwh_day"] - m["over_limit_kwh_day"]) / b["over_limit_kwh_day"]
                                                   if b["over_limit_kwh_day"] > 0 else None),
                      "revenue_change_pct": 100 * (m["revenue_day"] - b["revenue_day"]) / b["revenue_day"]})
            results[name].append(m)
    return results


def print_tables(res: dict, days: int, reps: int) -> None:
    for name, rows in res.items():
        print(f"\n=== Cenário {name} (demanda × {SCENARIOS[name]}) — {days} dias × {reps} repetições ===")
        print(f"{'tarifa':9}{'flex':>6}{'pico kW':>9}{'±dp':>6}{'Δpico':>8}{'kWh>limite/dia':>16}{'h>80%/dia':>11}"
              f"{'R$/dia':>9}{'ΔR$':>7}{'R$/kWh':>8}")
        for m in rows:
            print(f"{m['tarifa']:9}{m['flex']*100:5.0f}%{m['peak_kw']:9.1f}{m['peak_sd']:6.1f}{m['peak_reduction_pct']:7.1f}%"
                  f"{m['over_limit_kwh_day']:16.1f}{m['hours_above_80pct_day']:11.2f}{m['revenue_day']:9.0f}"
                  f"{m['revenue_change_pct']:6.1f}%{m['avg_price']:8.2f}")


def plot(res: dict) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib não instalado — gráficos pulados: pip install matplotlib)")
        return []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RED, INK, GREY, GREEN = "#C8102E", "#1a1a1a", "#9a9a9a", "#2a9d6f"
    files = []

    # 1) curva média do dia (cenário congestionado, flex 30%) + preço
    c = build_controller()
    rows = {m["flex"] if m["tarifa"] == "dinamica" else -1: m for m in res["congestionado"]}
    base, dyn = rows[-1], rows[0.30]
    usable = c.usable_limit_kw
    fig, ax = plt.subplots(figsize=(8, 4.2))
    hrs = list(range(24))
    ax.plot(hrs, base["curve"], color=GREY, lw=2.2, label="Preço fixo (sem tarifa dinâmica)")
    ax.plot(hrs, dyn["curve"], color=RED, lw=2.2, label="Tarifa dinâmica (30% de motoristas flexíveis)")
    ax.axhline(usable, color=INK, ls="--", lw=1)
    ax.text(23.5, usable + 1.5, f"limite utilizável {usable:.0f} kW", fontsize=8, ha="right")
    ax.set_ylim(0, 105)
    ax.set_xlabel("hora do dia"); ax.set_ylabel("demanda líquida da rede (kW)")
    ax.set_xticks(range(0, 24, 3)); ax.grid(alpha=.2)
    ax2 = ax.twinx()
    day = datetime.now().date()
    ax2.bar(hrs, day_prices(c, day), color=GREEN, alpha=.18, width=.8)
    ax2.set_ylabel("tarifa dinâmica de hoje (barras, R$/kWh)"); ax2.set_ylim(0, 4)
    ax.legend(loc="upper left", bbox_to_anchor=(0, 0.85), fontsize=8, frameon=False)
    ax.set_title("Dia médio — site congestionado (demanda × 1,8)", fontsize=11)
    fig.tight_layout(); p = OUT_DIR / "curva_dia_medio.png"; fig.savefig(p, dpi=160); plt.close(fig); files.append(p)

    # 2) sensibilidade: redução de pico x fração flexível, nos dois cenários
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8))
    for ax, key, title in ((axes[0], "peak_reduction_pct", "Redução do pico de demanda (%)"),
                           (axes[1], "revenue_change_pct", "Variação da receita (%)")):
        for name, color in (("normal", GREY), ("congestionado", RED)):
            pts = [m for m in res[name] if m["tarifa"] == "dinamica"]
            ax.plot([m["flex"] * 100 for m in pts], [m[key] for m in pts], marker="o", color=color, label=name)
        ax.axhline(0, color=INK, lw=.8); ax.set_xlabel("motoristas flexíveis (%)")
        ax.set_title(title, fontsize=10); ax.grid(alpha=.2)
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle("Sensibilidade à premissa de comportamento do motorista", fontsize=11)
    fig.tight_layout(); p = OUT_DIR / "sensibilidade.png"; fig.savefig(p, dpi=160); plt.close(fig); files.append(p)
    return files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--repeticoes", type=int, default=30)
    ap.add_argument("--semente", type=int, default=42)
    a = ap.parse_args()
    res = run(a.dias, a.repeticoes, a.semente)
    print_tables(res, a.dias, a.repeticoes)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slim = {n: [{k: v for k, v in m.items() if k != "curve"} for m in rows] for n, rows in res.items()}
    meta = {"dias": a.dias, "repeticoes": a.repeticoes, "semente": a.semente, "modelo_ia": model_metrics(),
            "premissas": {"janela_h": SHIFT_WINDOW_H, "saturacao_desconto": SATURATION_DISCOUNT,
                          "ruido_dia": DAY_SIGMA, "ruido_hora": HOUR_SIGMA, "cenarios": SCENARIOS}}
    (OUT_DIR / "resultados.json").write_text(json.dumps({"meta": meta, "resultados": slim}, indent=2, ensure_ascii=False))
    for f in plot(res):
        print("gráfico:", f)
    print("dados:", OUT_DIR / "resultados.json")


if __name__ == "__main__":
    main()
