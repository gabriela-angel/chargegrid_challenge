"""
ChargeGrid Intelligence — Solar fotovoltaico simulado
-------------------------------------------------------
Modelo propositalmente simples: uma curva em "sino" entre o nascer e o pôr do
sol, escalada pela potência de pico instalada (kWp). Sem nuvens, sem bateria.
É determinística (mesma hora → mesma geração), o que deixa a demo e os testes
reprodutíveis.

Na integração real, `pv_power_kw()` é substituída pela leitura do inversor
GoodWe (potência PV instantânea) — o resto do sistema só enxerga kW.

Fator de emissão da rede (CO2 evitado):
    O MCTI (base oficial do SIN) divulgou fatores mensais de 2025 entre
    ~0,021 e ~0,029 tCO2/MWh (jan–abr/2025). Usamos 0,025 kgCO2/kWh. É um
    número BAIXO porque a matriz elétrica brasileira já é muito renovável —
    por isso o argumento principal do solar aqui é redução de pico na rede e
    autoconsumo, e o CO2 evitado é apresentado como indicador secundário.
"""

import math
from datetime import datetime

SUNRISE_H = 6.0
SUNSET_H = 18.0
DEFAULT_PV_PEAK_KW = 40.0          # kWp instalados no estacionamento (cobertura)
GRID_EMISSION_KG_PER_KWH = 0.025   # kgCO2/kWh — ver docstring do módulo


def hour_float(moment: datetime) -> float:
    """Hora do dia como número real (14h30 → 14.5)."""
    return moment.hour + moment.minute / 60 + moment.second / 3600


def pv_power_kw(hour_of_day: float, peak_kw: float) -> float:
    """Potência FV (kW) numa hora do dia. Zero à noite; pico ao meio-dia solar."""
    if peak_kw <= 0 or hour_of_day <= SUNRISE_H or hour_of_day >= SUNSET_H:
        return 0.0
    x = (hour_of_day - SUNRISE_H) / (SUNSET_H - SUNRISE_H)   # 0 → 1 ao longo do dia
    return round(peak_kw * math.sin(math.pi * x) ** 1.5, 2)
