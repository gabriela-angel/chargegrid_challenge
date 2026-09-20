"""
ChargeGrid Intelligence — Engenharia de features (IA)
-----------------------------------------------------------
Fica num arquivo separado de propósito: tanto `train_model.py` (treina)
quanto `predictor.py` (prediz em produção) IMPORTAM esta mesma função.
Se cada um construísse as features do seu próprio jeito, um desalinhamento
sutil entre os dois (ex: esquecer de dividir por 24 num lugar só) quebra
o modelo silenciosamente — ele continua rodando, só que prevendo errado.
Ter uma função única elimina essa classe inteira de bug.

Conceito técnico — Features cíclicas com seno/cosseno:
Hora do dia (0 a 23) e dia da semana (0 a 6) são grandezas CÍCLICAS: a
hora 23 está "colada" na hora 0 do dia seguinte, não a 23 unidades de
distância como um número comum sugeriria. Se passarmos "hour" cru pra
uma regressão linear, o modelo entende 23h e 0h como extremos opostos,
quando na prática são vizinhas. A correção padrão é projetar a hora num
círculo usando seno e cosseno: sin(2π·hora/24) e cos(2π·hora/24) —
juntos, esses dois números identificam univocamente cada hora, e hora
23 fica matematicamente perto de hora 0, como devia.

Por que TAMBÉM incluímos um "segundo harmônico" (período de 12h, não só
24h): a demanda do site tem DOIS picos por dia (manhã e noite) — uma
curva senoidal simples de período 24h só consegue representar UM pico e
UM vale por dia. Adicionar sin/cos com período de 12h dá ao modelo
linear "vocabulário" pra somar duas ondas e formar uma curva de dois
picos, sem deixar de ser uma regressão linear simples por baixo dos
panos (LinearRegression continua só somando features com pesos —
a não-linearidade mora inteira na transformação de features, não no
modelo).
"""

import math

FEATURE_NAMES = [
    "hour_sin_24", "hour_cos_24",
    "hour_sin_12", "hour_cos_12",
    "dow_sin", "dow_cos",
    "is_weekend",
]


def build_features(hour: int, day_of_week: int) -> list[float]:
    """
    hour: 0-23
    day_of_week: 0 (segunda) a 6 (domingo) — padrão datetime.weekday()
    """
    is_weekend = 1.0 if day_of_week >= 5 else 0.0
    return [
        math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
        math.sin(2 * math.pi * hour / 12), math.cos(2 * math.pi * hour / 12),
        math.sin(2 * math.pi * day_of_week / 7), math.cos(2 * math.pi * day_of_week / 7),
        is_weekend,
    ]
