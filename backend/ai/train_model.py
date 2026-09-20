"""
ChargeGrid Intelligence — Treino do modelo de predição de demanda
-----------------------------------------------------------------------
Treina uma regressão linear simples (scikit-learn) que aprende o
"formato do dia" do site: dado (hora, dia da semana), prevê a potência
total demandada (kW) — pra o operador ver de manhã "olha, hoje às 19h a
gente deve bater perto do limite contratado" ANTES de bater, e não só
reagir depois que já bateu.

Fontes de dado, combinadas:
  1. ai/data/historico_simulado.csv — histórico sintético (ver
     generate_history.py), usado enquanto o projeto é novo e não tem
     meses de uso real acumulado.
  2. controller.completed_sessions — histórico REAL (pode estar vazio
     ou pequeno agora; cresce sozinho conforme o site é usado). Cada
     sessão concluída vira 1 amostra: potência média = energia (kWh) /
     duração (h), na hora em que a sessão começou.

Por que aproximar "potência média da sessão inteira" como se fosse "a
demanda daquela 1 hora": não guardamos (ainda) uma série temporal
minuto-a-minuto de potência do site — só o resumo de cada sessão
(started_at, duration_min, energy_kwh). Pra um MVP, tratar a sessão
inteira como uma amostra na hora em que começou é uma simplificação
aceitável — dá pro modelo aprender o padrão geral. Se o projeto crescer,
o upgrade natural é logar `total_allocated_kw` do site a cada tick (já
calculado em tempo real por core/demand_controller.py) numa tabela de
série temporal no Postgres, e treinar em cima disso em vez desta
aproximação.

Rodar (de dentro da pasta chargegrid_backend, com o venv ativado):
    python -m ai.train_model
"""

import csv
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score

from ai.features import build_features, FEATURE_NAMES

DATA_PATH = Path(__file__).parent / "data" / "historico_simulado.csv"
MODEL_PATH = Path(__file__).parent / "model.joblib"


def _load_synthetic_rows() -> list[dict]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"{DATA_PATH} não existe. Rode primeiro: python -m ai.generate_history"
        )
    with open(DATA_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_real_rows() -> list[dict]:
    """
    Converte controller.completed_sessions (histórico REAL, do mesmo
    processo — este script roda dentro do backend, então importa o
    controller diretamente, sem precisar de HTTP) em linhas no mesmo
    formato do CSV sintético.
    """
    from core.state import controller  # import local: evita custo se não for usado

    rows = []
    for session in controller.completed_sessions:
        duration_h = session.duration_minutes() / 60
        if duration_h <= 0:
            continue  # sessão de teste/instantânea — não informativa pro modelo
        avg_kw = session.energy_consumed_kwh / duration_h
        rows.append({
            "hour": session.started_at.hour,
            "day_of_week": session.started_at.weekday(),
            "demand_kw": avg_kw,
        })
    return rows


def build_dataset() -> tuple[np.ndarray, np.ndarray]:
    rows = _load_synthetic_rows()
    real_rows = _load_real_rows()
    if real_rows:
        print(f"Misturando {len(real_rows)} sessões reais ao histórico sintético.")
        rows = rows + real_rows

    X = [build_features(int(r["hour"]), int(r["day_of_week"])) for r in rows]
    y = [float(r["demand_kw"]) for r in rows]
    return np.array(X), np.array(y)


def main():
    X, y = build_dataset()
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = LinearRegression()
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    mae = mean_absolute_error(y_test, y_pred)
    r2 = r2_score(y_test, y_pred)

    print(f"Amostras de treino: {len(X_train)} | teste: {len(X_test)}")
    print(f"MAE  (erro médio absoluto): {mae:.2f} kW")
    print(f"R²   (quanto da variação o modelo explica): {r2:.3f}")
    print("Coeficientes por feature:")
    for name, coef in zip(FEATURE_NAMES, model.coef_):
        print(f"  {name:>12}: {coef:+.3f}")

    joblib.dump({"model": model, "feature_names": FEATURE_NAMES}, MODEL_PATH)
    print(f"\nModelo salvo em {MODEL_PATH}")


if __name__ == "__main__":
    main()
