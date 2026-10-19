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
  2. Demanda REAL do site: o DemandController registra o `total_allocated_kw`
     a cada segundo e agrega por (dia, hora) — `hourly_demand_samples()`.
     Cada hora com dado suficiente vira 1 amostra com a MESMA definição do
     alvo sintético (potência total do site naquela hora).

O treino com dado real roda DENTRO do backend: `POST /api/predictions/retrain`
chama `train(real_rows=...)` com os dados do controller em memória. (A versão
anterior importava o controller num processo novo, onde o histórico sempre
estava vazio — por isso "misturar sessões reais" nunca acontecia de fato.)
Pela linha de comando (`python -m ai.train_model`) treina só com o sintético.

Rodar (de dentro da pasta chargegrid_backend, com o venv ativado):
    python -m ai.train_model
"""

import csv
from datetime import datetime
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


def build_dataset(real_rows: list[dict] | None = None) -> tuple[np.ndarray, np.ndarray]:
    rows = _load_synthetic_rows()
    if real_rows:
        print(f"Misturando {len(real_rows)} amostras horárias reais ao histórico sintético.")
        rows = rows + real_rows

    X = [build_features(int(r["hour"]), int(r["day_of_week"])) for r in rows]
    y = [float(r["demand_kw"]) for r in rows]
    return np.array(X), np.array(y)


def train(real_rows: list[dict] | None = None, verbose: bool = True) -> dict:
    """Treina, salva ai/model.joblib e devolve as métricas (MAE, R², amostras)."""
    X, y = build_dataset(real_rows)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = LinearRegression()
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    metrics = {
        "mae_kw": round(float(mean_absolute_error(y_test, y_pred)), 2),
        "r2": round(float(r2_score(y_test, y_pred)), 3),
        "train_samples": int(len(X_train)),
        "test_samples": int(len(X_test)),
        "real_samples": len(real_rows or []),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "coefficients": {n: round(float(c), 3) for n, c in zip(FEATURE_NAMES, model.coef_)},
    }

    if verbose:
        print(f"Amostras de treino: {metrics['train_samples']} | teste: {metrics['test_samples']}")
        print(f"MAE  (erro médio absoluto): {metrics['mae_kw']:.2f} kW")
        print(f"R²   (quanto da variação o modelo explica): {metrics['r2']:.3f}")
        print("Coeficientes por feature:")
        for name, coef in metrics["coefficients"].items():
            print(f"  {name:>12}: {coef:+.3f}")

    joblib.dump({"model": model, "feature_names": FEATURE_NAMES, "metrics": metrics}, MODEL_PATH)
    if verbose:
        print(f"\nModelo salvo em {MODEL_PATH}")
    return metrics


def main():
    train()


if __name__ == "__main__":
    main()
