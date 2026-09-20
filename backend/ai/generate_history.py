"""
ChargeGrid Intelligence — Gerador de histórico sintético
------------------------------------------------------------
Problema que este arquivo resolve: pra treinar um modelo de predição de
demanda, precisamos de MESES de histórico — mas o projeto começou agora,
e o `GET /api/sessions` real está praticamente vazio. Sem dado, não tem
o que a IA aprenda (nem tem o que demonstrar no pitch/entrega).

Solução: gerar um histórico PLAUSÍVEL de demanda por hora, baseado em
como um estacionamento de shopping realmente se comporta:

  - dois picos por dia (gente chegando de manhã ~8h, gente saindo/
    carregando à noite ~19h antes de ir embora)
  - fim de semana mais fraco e mais espalhado (sem os dois picos de
    "horário de trabalho")
  - ruído aleatório (dia nenhum é igual ao outro)

Isso não é "inventar dado pra enganar ninguém" — é a mesma técnica que
qualquer time de ML usa quando não tem histórico real ainda
(chama-se dado sintético/simulado, e o próprio projeto já documenta isso
em todo lugar: "coleta de dados simulada via WiFi/HTTP"). O objetivo é
ter uma CURVA REALISTA pra o modelo aprender o formato geral (dois
picos, vale de madrugada), não os números exatos — esses vão sendo
corrigidos conforme dado real (de verdade) for se acumulando em
`GET /api/sessions` (ver train_model.py, que já deixa isso pronto pra
misturar os dois).

Rodar:
    python -m ai.generate_history
"""

import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

OUTPUT_PATH = Path(__file__).parent / "data" / "historico_simulado.csv"
DIAS_DE_HISTORICO = 90       # ~3 meses
USABLE_LIMIT_KW = 95.0       # mesmo limite útil do core/state.py (100kW - 5% margem)


def _demanda_base(hour: float, is_weekend: bool) -> float:
    """
    Curva-base de demanda (kW) pra uma hora do dia (0-23.99).

    Modelada como soma de duas "colinas" (gaussianas) — uma de manhã,
    uma de noite — porque é isso que o formato real de um estacionamento
    costuma parecer: gente chega, carrega um pouco, gente sai carregando
    mais antes de ir pra casa. Fim de semana é mais fraco e mais achatado
    (sem os dois picos "de expediente").
    """
    def colina(centro, largura, altura):
        return altura * math.exp(-((hour - centro) ** 2) / (2 * largura ** 2))

    if is_weekend:
        # sem os dois picos de dia útil — um platô mais largo e mais baixo
        # à tarde (passeio de fim de semana), sem pico de manhã cedo
        return colina(14, 4.0, 28) + 4  # piso mínimo (alguém sempre carregando)

    pico_manha = colina(8, 2.5, 35)
    pico_noite = colina(19, 3.0, 52)   # o mais forte — saída do trabalho
    piso = 6  # nunca zera de madrugada (carros noturnos, entregas)
    return pico_manha + pico_noite + piso


def gerar_historico(dias: int = DIAS_DE_HISTORICO) -> list[dict]:
    linhas = []
    inicio = datetime.now() - timedelta(days=dias)

    for d in range(dias):
        dia = inicio + timedelta(days=d)
        is_weekend = dia.weekday() >= 5  # 5=sábado, 6=domingo

        for hour in range(24):
            base = _demanda_base(hour, is_weekend)
            # ruído: dia nenhum é idêntico ao outro (±15%, mais um pouco
            # de variação absoluta pra não ficar sempre proporcional)
            ruido = random.gauss(0, base * 0.15 + 2)
            demanda_kw = max(0.0, min(USABLE_LIMIT_KW, base + ruido))

            linhas.append({
                "timestamp": dia.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat(),
                "hour": hour,
                "day_of_week": dia.weekday(),
                "is_weekend": int(is_weekend),
                "demand_kw": round(demanda_kw, 2),
            })

    return linhas


def main():
    linhas = gerar_historico()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "hour", "day_of_week", "is_weekend", "demand_kw"])
        writer.writeheader()
        writer.writerows(linhas)
    print(f"Gerado: {len(linhas)} linhas ({DIAS_DE_HISTORICO} dias x 24h) -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
