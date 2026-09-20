"""
ChargeGrid Intelligence — Schemas da API
------------------------------------------
Contratos de request/response. Mantidos simples de propósito:
o ESP32 (C++/Arduino) precisa montar esse JSON à mão, então quanto
mais enxuto, mais fácil de implementar no firmware depois.
"""

from datetime import datetime

from pydantic import BaseModel


class ConnectRequest(BaseModel):
    vehicle_id: str


class ChargerOut(BaseModel):
    id: str
    location: str
    max_kw: float
    status: str  # "LIVRE" | "OCUPADO"
    allocated_kw: float
    vehicle_id: str | None = None
    duration_min: float | None = None
    energy_kwh: float | None = None


class SiteStateOut(BaseModel):
    site: str
    contracted_limit_kw: float
    usable_limit_kw: float
    usable_limit_pct: float
    total_allocated_kw: float
    available_kw: float
    utilization_pct: float
    active_sessions: int
    chargers: list[ChargerOut]
    events: list[dict]


class SessionOut(BaseModel):
    charger_id: str
    vehicle_id: str
    started_at: str  # ISO 8601
    duration_min: float
    energy_kwh: float


class EventOut(BaseModel):
    timestamp: str
    type: str
    source: str
    message: str


class DisconnectResult(BaseModel):
    charger_id: str
    vehicle_id: str
    energy_kwh: float
    duration_min: float
    # placeholder — pilar de Tarifação ainda não implementado
    estimated_cost_brl: float | None = None


class SettingsIn(BaseModel):
    """Campos opcionais: manda só o que quer mudar (o outro fica como está)."""
    contracted_limit_kw: float | None = None
    usable_limit_pct: float | None = None


class SettingsOut(BaseModel):
    contracted_limit_kw: float
    usable_limit_pct: float          # % da demanda contratada usável antes de reduzir a potência
    usable_limit_kw: float           # contracted_limit_kw × usable_limit_pct / 100
    min_usable_limit_pct: float
    max_usable_limit_pct: float


class SessionImportItem(BaseModel):
    """Uma sessão JÁ CONCLUÍDA a importar para o histórico."""
    charger_id: str
    vehicle_id: str
    started_at: datetime
    duration_min: float
    energy_kwh: float


class SessionImportRequest(BaseModel):
    sessions: list[SessionImportItem]


class SessionImportResult(BaseModel):
    imported: int
    skipped: int  # já existiam (mesmo carregador + veículo + início)
