"""
ChargeGrid Intelligence — Router de Carregadores
---------------------------------------------------
Estes 4 endpoints são o ÚNICO contrato entre o backend e QUALQUER
cliente: o ESP32 físico, o dashboard do operador, o app do motorista,
ou um script de simulação no terminal. Todos falam a mesma língua.

    GET  /api/chargers                     → estado completo do site
    GET  /api/chargers/{id}                → estado de 1 carregador
    POST /api/chargers/{id}/connect        → inicia sessão (evento)
    POST /api/chargers/{id}/disconnect     → encerra sessão (evento)

Por que GET separado do POST de connect/disconnect?
O ESP32 dispara o POST só quando MUDA algo (carro chegou/saiu) —
igual um evento físico real. Mas a potência alocada a ELE pode mudar
a qualquer momento por causa de OUTRO carregador (redistribuição).
Por isso ele (e os frontends) fazem polling no GET a cada poucos
segundos, só para saber "quanto eu posso puxar agora".
"""

from fastapi import APIRouter, HTTPException

from core.state import controller
from schemas import (ConnectRequest, SiteStateOut, ChargerOut, DisconnectResult, SessionOut, EventOut,
                     SessionImportRequest, SessionImportResult)

router = APIRouter(prefix="/api/chargers", tags=["chargers"])

# Router separado pra não misturar "carregador individual" com "coleções
# do site inteiro" na URL — usado pelo dashboard do operador (histórico)
# e pelo frontend (BFF) pra montar as telas de faturamento/relatórios.
site_router = APIRouter(prefix="/api", tags=["site"])


@site_router.get("/sessions", response_model=list[SessionOut])
def get_completed_sessions():
    """
    Histórico de sessões já finalizadas — o que faltava pro frontend
    montar telas de faturamento/histórico sem guardar nenhum estado
    próprio. Vem direto de controller.completed_sessions (mesma lista
    que _state_snapshot() usa para compor o resto da resposta).
    """
    return [
        SessionOut(
            charger_id=s.charger_id,
            vehicle_id=s.vehicle_id,
            started_at=s.started_at.isoformat(),
            duration_min=round(s.duration_minutes(), 1),
            energy_kwh=round(s.energy_consumed_kwh, 3),
        )
        for s in controller.completed_sessions
    ]


@site_router.post("/sessions/import", response_model=SessionImportResult)
def import_completed_sessions(body: SessionImportRequest):
    """
    Importa sessões JÁ CONCLUÍDAS para o histórico — usado para popular a
    demo com recargas anteriores dos clientes fictícios (e útil numa
    migração de histórico). Não mexe em carregadores nem redistribui
    potência. Idempotente: repetir o envio não duplica.
    """
    items = [
        {
            "charger_id": s.charger_id,
            "vehicle_id": s.vehicle_id,
            "started_at": s.started_at.replace(tzinfo=None),  # o controller trabalha com datetimes ingênuos (hora local)
            "duration_min": s.duration_min,
            "energy_kwh": s.energy_kwh,
        }
        for s in body.sessions
    ]
    try:
        return controller.import_completed_sessions(items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@site_router.get("/events", response_model=list[EventOut])
def get_events(source: str | None = None, limit: int = 20):
    """
    Log de eventos completo (não só os últimos 5 que vêm embutidos em
    /api/chargers). `source` filtra por carregador (ex: CG-01) ou
    'SYSTEM'; `limit` corta pro final (mais recentes).
    """
    events = controller.events
    if source:
        events = [e for e in events if e["source"] == source]
    return events[-limit:]


@router.get("", response_model=SiteStateOut)
def get_site_state():
    """Estado completo do site — usado pelo dashboard do operador."""
    return controller._state_snapshot()


@router.get("/{charger_id}", response_model=ChargerOut)
def get_charger(charger_id: str):
    """
    Estado de um único carregador — usado pelo ESP32 (polling) e pelo
    app do motorista (que só se importa com o carregador dele).
    """
    state = controller._state_snapshot()
    for c in state["chargers"]:
        if c["id"] == charger_id:
            return c
    raise HTTPException(status_code=404, detail=f"Carregador '{charger_id}' não encontrado.")


@router.post("/{charger_id}/connect", response_model=ChargerOut)
def connect_vehicle(charger_id: str, body: ConnectRequest):
    """
    Evento: veículo conectou. Dispara a redistribuição de potência
    em TODOS os carregadores ativos (não só neste).
    """
    result = controller.vehicle_connect(charger_id, body.vehicle_id)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])

    for c in result["chargers"]:
        if c["id"] == charger_id:
            return c
    raise HTTPException(status_code=404, detail=f"Carregador '{charger_id}' não encontrado.")


@router.post("/{charger_id}/disconnect", response_model=DisconnectResult)
def disconnect_vehicle(charger_id: str):
    """
    Evento: veículo desconectou. Fecha a sessão, libera a potência e
    redistribui para os demais. Retorna o resumo (kWh, duração) —
    o cálculo de custo (tarifa dinâmica) entra aqui quando o pilar de
    Tarifação for implementado.
    """
    # Precisamos do vehicle_id e da sessão ANTES de desconectar,
    # então lemos o snapshot atual primeiro.
    state_before = controller._state_snapshot()
    charger_before = next((c for c in state_before["chargers"] if c["id"] == charger_id), None)
    if charger_before is None:
        raise HTTPException(status_code=404, detail=f"Carregador '{charger_id}' não encontrado.")
    if charger_before["status"] != "OCUPADO":
        raise HTTPException(status_code=409, detail=f"Carregador '{charger_id}' não tem sessão ativa.")

    vehicle_id = charger_before["vehicle_id"]

    result = controller.vehicle_disconnect(charger_id)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])

    # A sessão finalizada é a última adicionada em completed_sessions.
    finished = controller.completed_sessions[-1]

    return DisconnectResult(
        charger_id=charger_id,
        vehicle_id=vehicle_id,
        energy_kwh=round(finished.energy_consumed_kwh, 3),
        duration_min=round(finished.duration_minutes(), 1),
        estimated_cost_brl=None,
    )
