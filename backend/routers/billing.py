"""
ChargeGrid Intelligence — Router de Faturamento
-------------------------------------------------
    GET /api/billing/series?period=day|week|month
        → barras de faturamento (R$, kWh e nº de sessões por hora ou por dia),
          total do período e comparação com o período anterior.
"""

from typing import Literal

from fastapi import APIRouter

from core.state import controller

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/series")
def billing_series(period: Literal["day", "week", "month"] = "day") -> dict:
    return controller.billing_series(period)
