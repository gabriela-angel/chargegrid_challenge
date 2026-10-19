"""
ChargeGrid Intelligence — Router do hardware (Modbus)
-------------------------------------------------------
    GET /api/hardware  → estado do link Modbus e, para cada sessão ativa, o que o
                         controller alocou x o que o carregador realmente aplicou
                         (lido de volta dos registradores 10029 e 10015).

É a prova de que a escrita chegou ao equipamento — útil na demo e no vídeo.
"""

from fastapi import APIRouter

from core.hardware import HardwareError
from core.state import controller

router = APIRouter(prefix="/api/hardware", tags=["hardware"])


@router.get("")
def hardware_state() -> dict:
    link = controller.hardware
    if link is None:
        return {"enabled": False, "mode": "off", "chargers": []}

    rows = []
    for session in controller.active_sessions:
        row = {"charger_id": session.charger_id, "allocated_kw": session.allocated_kw}
        try:
            row["limit_reg_10029_kw"] = link.read_limit(session.charger_id)
            row["power_reg_10015_kw"] = link.read_power(session.charger_id)
            row["in_sync"] = abs(row["limit_reg_10029_kw"] - session.allocated_kw) < 0.1
        except HardwareError as exc:
            row["error"] = str(exc)
            row["in_sync"] = False
        rows.append(row)
    return {"enabled": True, **link.status(), "chargers": rows}
