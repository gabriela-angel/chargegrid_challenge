"""
ChargeGrid Intelligence — Router de Tarifa Dinâmica
------------------------------------------------------
    GET /api/tariff                     → preço do kWh agora (com a pressão que o gerou)
    GET /api/tariff/proximas-horas?n=24 → curva de preço hora a hora + melhor horário
    GET /api/tariff/config              → parâmetros vigentes (base, multiplicadores, limiares)
    PUT /api/tariff/config              → altera parâmetros (só o que vier no corpo)

O preço é calculado a partir da previsão de demanda da IA (ver core/tariff.py).
"""

from fastapi import APIRouter, HTTPException

from core.state import controller
from schemas import TariffConfigIn

router = APIRouter(prefix="/api/tariff", tags=["tariff"])


@router.get("")
def get_tariff():
    return controller.pricing_now()


@router.get("/proximas-horas")
def get_tariff_forecast(n: int = 24):
    n = max(1, min(n, 48))
    curve = controller.pricing_forecast(n)
    now_price = controller.pricing_now()["price_rs_per_kwh"]
    cheapest = min(curve, key=lambda p: (p["price_rs_per_kwh"], p["hour"]))
    peak = max(curve, key=lambda p: p["price_rs_per_kwh"])
    saving_pct = round(100 * (now_price - cheapest["price_rs_per_kwh"]) / now_price, 1) if now_price else 0.0
    return {
        "now": controller.pricing_now(),
        "forecast": curve,
        "cheapest_hour": cheapest["hour"],
        "cheapest_price_rs_per_kwh": cheapest["price_rs_per_kwh"],
        "peak_hour": peak["hour"],
        "peak_price_rs_per_kwh": peak["price_rs_per_kwh"],
        "saving_vs_now_pct": max(0.0, saving_pct),
    }


@router.get("/config")
def get_tariff_config():
    return controller.tariff_config.as_dict()


@router.put("/config")
def update_tariff_config(body: TariffConfigIn):
    result = controller.update_tariff_config(**body.model_dump())
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result
