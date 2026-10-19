"""
ChargeGrid Intelligence — Tarifa dinâmica (demand response)
-------------------------------------------------------------
A IA já prevê a demanda do site hora a hora. Aqui essa previsão vira PREÇO:
quanto maior a pressão prevista sobre a rede, mais cara a tarifa — o que
estimula o motorista a carregar em horários mais folgados e evita sobrecarga.

    pressão (%)  = demanda líquida da rede ÷ limite utilizável × 100
    demanda líquida = max(0, demanda − geração solar)      ← sol alivia a rede
    multiplicador = interpolação linear entre `min_multiplier` (pressão ≤ low)
                    e `max_multiplier` (pressão ≥ high)
    preço (R$/kWh) = tarifa-base × multiplicador

Calibragem de mercado (ago/2026, VRUM/Dok): recarga rápida DC pública custa
R$ 1,80–2,50/kWh e a energia residencial ~R$ 0,85/kWh. Base de R$ 1,90 com
multiplicador 0,75–1,30 gera preços de R$ 1,43 a R$ 2,47 — dentro da faixa.

`low/high_pressure_pct` vêm do histórico: ~P10 e ~P90 da demanda do site
(a curva sintética vai de ~11% a ~58% do limite útil), por isso high = 55%.

O preço da sessão é TRAVADO na conexão (o que o motorista viu é o que paga),
mesmo que a tarifa mude durante a recarga.
"""

from dataclasses import dataclass, asdict


@dataclass
class TariffConfig:
    base_rs_per_kwh: float = 1.90
    min_multiplier: float = 0.75
    max_multiplier: float = 1.30
    low_pressure_pct: float = 15.0
    high_pressure_pct: float = 55.0
    # True: os dois limiares de pressão são calibrados pela IA (percentis da demanda
    # prevista) a cada re-treino. Editar os limiares à mão desliga isso.
    pressure_auto: bool = True

    def as_dict(self) -> dict:
        d = asdict(self)
        d["min_price_rs_per_kwh"] = round(self.base_rs_per_kwh * self.min_multiplier, 2)
        d["max_price_rs_per_kwh"] = round(self.base_rs_per_kwh * self.max_multiplier, 2)
        return d


def validate(cfg: TariffConfig) -> str | None:
    """Devolve a mensagem de erro (ou None se a configuração é válida)."""
    if cfg.base_rs_per_kwh <= 0:
        return "A tarifa-base deve ser maior que zero."
    if not (0 < cfg.min_multiplier <= 1.0 <= cfg.max_multiplier <= 3.0):
        return "Multiplicadores devem satisfazer 0 < mínimo ≤ 1 ≤ máximo ≤ 3."
    if not (0 <= cfg.low_pressure_pct < cfg.high_pressure_pct <= 150):
        return "A pressão baixa deve ser menor que a alta (em %, até 150)."
    return None


def multiplier_for(cfg: TariffConfig, pressure_pct: float) -> float:
    span = cfg.high_pressure_pct - cfg.low_pressure_pct
    t = (pressure_pct - cfg.low_pressure_pct) / span
    t = max(0.0, min(1.0, t))
    return cfg.min_multiplier + t * (cfg.max_multiplier - cfg.min_multiplier)


def level_for(multiplier: float) -> str:
    """'baixa' | 'normal' | 'alta' — rótulo para a interface."""
    if multiplier < 0.95:
        return "baixa"
    if multiplier > 1.10:
        return "alta"
    return "normal"


def price_for(cfg: TariffConfig, pressure_pct: float) -> dict:
    mult = multiplier_for(cfg, pressure_pct)
    return {
        "pressure_pct": round(pressure_pct, 1),
        "multiplier": round(mult, 3),
        "price_rs_per_kwh": round(cfg.base_rs_per_kwh * mult, 2),
        "level": level_for(mult),
    }
