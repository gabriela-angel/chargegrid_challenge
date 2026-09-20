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

from ai.predictor import predict_next_hours
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
