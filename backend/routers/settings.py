"""
ChargeGrid Intelligence — Router de Configurações do Site
-----------------------------------------------------------
    GET /api/settings   → demanda contratada + limite de uso atuais
    PUT /api/settings   → altera um ou os dois (só o que vier no corpo)

"Limite de uso" é a % da demanda contratada que a rede pode consumir antes
de o controlador reduzir a potência dos carregadores (default 90%, teto 95%
— ver core/demand_controller.py). Alterar qualquer um dos dois redistribui
a potência das sessões ativas imediatamente.
"""

from fastapi import APIRouter, HTTPException

from core.demand_controller import MAX_USABLE_LIMIT_PCT, MIN_USABLE_LIMIT_PCT
from core.state import controller
from schemas import SettingsIn, SettingsOut

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _settings_out() -> SettingsOut:
    return SettingsOut(
        contracted_limit_kw=controller.contracted_limit_kw,
        usable_limit_pct=controller.usable_limit_pct,
        usable_limit_kw=round(controller.usable_limit_kw, 2),
        min_usable_limit_pct=MIN_USABLE_LIMIT_PCT,
        max_usable_limit_pct=MAX_USABLE_LIMIT_PCT,
    )


@router.get("", response_model=SettingsOut)
def get_settings():
    return _settings_out()


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsIn):
    result = controller.update_settings(
        contracted_limit_kw=body.contracted_limit_kw,
        usable_limit_pct=body.usable_limit_pct,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return _settings_out()
