"""
ChargeGrid Intelligence — Router de Energia Solar (simulada)
---------------------------------------------------------------
    GET /api/solar         → geração FV agora e indicadores de energia verde
    PUT /api/solar/config  → potência instalada (kWp) e/ou hora solar "congelada" (demo)
"""

from fastapi import APIRouter, HTTPException

from core.solar import GRID_EMISSION_KG_PER_KWH
from core.state import controller
from schemas import SolarConfigIn

router = APIRouter(prefix="/api/solar", tags=["solar"])


def _solar_out() -> dict:
    return {
        "solar_kw": round(controller.pv_kw, 2),
        "solar_peak_kw": controller.pv_peak_kw,
        "sim_hour": controller.sim_hour,
        "budget_kw": round(controller.budget_kw, 2),
        "grid_draw_kw": round(controller.grid_draw_kw, 2),
        "green_kwh_total": round(controller.total_green_kwh, 3),
        "green_share_pct": round(controller.green_share_pct, 1),
        "co2_avoided_kg": round(controller.co2_avoided_kg, 4),
        "grid_emission_kg_per_kwh": GRID_EMISSION_KG_PER_KWH,
    }


@router.get("")
def get_solar():
    return _solar_out()


@router.put("/config")
def update_solar(body: SolarConfigIn):
    result = controller.update_solar(
        peak_kw=body.peak_kw,
        sim_hour=body.sim_hour,
        set_sim_hour="sim_hour" in body.model_fields_set,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return _solar_out()
