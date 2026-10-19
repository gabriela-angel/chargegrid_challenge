"""
ChargeGrid Intelligence — Router de Predição de Demanda
-------------------------------------------------------------
    GET /api/predictions/proximas-horas?n=24

Devolve a previsão de potência demandada (kW) para as próximas N horas,
já comparada com o limite útil contratado (usable_limit_kw) — pra virar
direto um alerta visual no dashboard ("83% do limite previsto às 19h")
sem o frontend precisar saber a conta.
"""

from fastapi import APIRouter, HTTPException

from ai.predictor import model_metrics, predict_next_hours, reload_model
from ai.train_model import train
from core.state import controller

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


@router.get("/proximas-horas")
def get_predictions(n: int = 24):
    try:
        forecast = predict_next_hours(n=n)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    usable_limit_kw = controller.usable_limit_kw
    for point in forecast:
        point["usable_limit_kw"] = usable_limit_kw
        point["pct_of_limit"] = round(100 * point["predicted_kw"] / usable_limit_kw, 1) if usable_limit_kw else 0.0

    return {"usable_limit_kw": usable_limit_kw, "forecast": forecast}


@router.get("/modelo")
def get_model_info():
    """Métricas do modelo em uso (MAE, R², amostras reais usadas no treino)."""
    try:
        return model_metrics()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/retrain")
def retrain():
    """
    Re-treina a IA misturando ao histórico sintético (1) a demanda medida ao vivo
    pelo controller (média horária de total_allocated_kw; só horas com ao menos 60
    amostras) e (2) a demanda reconstruída das sessões já concluídas — o histórico
    importado vira dado de treino.
    """
    real_rows = controller.hourly_demand_samples() + controller.session_demand_samples()
    metrics = train(real_rows=real_rows, verbose=False)
    reload_model()
    metrics["tariff_pressure"] = controller.calibrate_tariff_pressure()   # re-calibra a tarifa com o modelo novo
    return metrics
