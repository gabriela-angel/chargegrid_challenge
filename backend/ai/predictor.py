"""
ChargeGrid Intelligence — Servindo previsões
-----------------------------------------------
Carrega ai/model.joblib UMA VEZ (na primeira chamada) e mantém em
memória — treinar de novo a cada requisição seria lento à toa, já que o
modelo só muda quando alguém roda `python -m ai.train_model` de novo
(manualmente, ou depois futuramente por um job agendado).
"""

from datetime import datetime, timedelta
from pathlib import Path

import joblib

from ai.features import build_features

MODEL_PATH = Path(__file__).parent / "model.joblib"

_cache: dict = {}


def _get_model():
    if "model" not in _cache:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                "Modelo ainda não treinado. Rode: python -m ai.generate_history "
                "&& python -m ai.train_model"
            )
        bundle = joblib.load(MODEL_PATH)
        _cache["model"] = bundle["model"]
        _cache["metrics"] = bundle.get("metrics", {})
    return _cache["model"]


def reload_model() -> None:
    """Descarta o modelo em cache — a próxima previsão relê ai/model.joblib (após re-treino)."""
    _cache.clear()


def model_metrics() -> dict:
    _get_model()
    return _cache.get("metrics", {})


def predict_hour(hour: int, day_of_week: int) -> float:
    model = _get_model()
    features = [build_features(hour, day_of_week)]
    predicted = model.predict(features)[0]
    return max(0.0, round(float(predicted), 2))  # demanda nunca é negativa


def predict_next_hours(n: int = 24, start: datetime | None = None) -> list[dict]:
    """
    Previsão para as próximas `n` horas a partir de `start` (ou agora).
    Usado pelo endpoint /api/predictions/proximas-horas — o que o
    dashboard do operador desenha como o gráfico de previsão.
    """
    start = start or datetime.now()
    result = []
    for i in range(n):
        moment = start + timedelta(hours=i)
        result.append({
            "timestamp": moment.replace(minute=0, second=0, microsecond=0).isoformat(),
            "hour": moment.hour,
            "predicted_kw": predict_hour(moment.hour, moment.weekday()),
        })
    return result
