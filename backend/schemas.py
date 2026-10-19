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
    # Preço (R$/kWh) que o motorista viu e aceitou — fica travado na sessão.
    # Omitido (ESP32, operador): vale o preço dinâmico do momento.
    tariff_rs_per_kwh: float | None = None


class ChargerOut(BaseModel):
    id: str
    location: str
    max_kw: float
    status: str  # "LIVRE" | "OCUPADO"
    allocated_kw: float
    vehicle_id: str | None = None
    duration_min: float | None = None
    energy_kwh: float | None = None
    tariff_rs_per_kwh: float | None = None
    cost_rs: float | None = None
    green_kwh: float | None = None


class SiteStateOut(BaseModel):
    site: str
    contracted_limit_kw: float
    usable_limit_kw: float
    usable_limit_pct: float
    total_allocated_kw: float
    available_kw: float
    utilization_pct: float
    active_sessions: int
    solar_kw: float = 0.0
    solar_peak_kw: float = 0.0
    sim_hour: float | None = None
    budget_kw: float = 0.0
    grid_draw_kw: float = 0.0
    tariff: dict = {}
    green_kwh_total: float = 0.0
    green_share_pct: float = 0.0
    co2_avoided_kg: float = 0.0
    chargers: list[ChargerOut]
    events: list[dict]


class SessionOut(BaseModel):
    charger_id: str
    vehicle_id: str
    started_at: str  # ISO 8601
    duration_min: float
    energy_kwh: float
    tariff_rs_per_kwh: float = 0.0
    cost_rs: float = 0.0
    green_kwh: float = 0.0


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
    tariff_rs_per_kwh: float = 0.0
    cost_brl: float = 0.0
    green_kwh: float = 0.0


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
    tariff_rs_per_kwh: float | None = None   # omitido: reconstrói o preço dinâmico da hora
    green_kwh: float | None = None           # omitido: estima pelo solar do período


class SessionImportRequest(BaseModel):
    sessions: list[SessionImportItem]


class SessionImportResult(BaseModel):
    imported: int
    skipped: int  # já existiam (mesmo carregador + veículo + início)


class TariffConfigIn(BaseModel):
    """Campos opcionais: manda só o que quer mudar."""
    base_rs_per_kwh: float | None = None
    min_multiplier: float | None = None
    max_multiplier: float | None = None
    low_pressure_pct: float | None = None
    high_pressure_pct: float | None = None
    pressure_auto: bool | None = None   # True: volta à calibração automática pela IA


class SolarConfigIn(BaseModel):
    """peak_kw: kWp instalados. sim_hour: hora "congelada" para demo (0–24);
    mandar `sim_hour: null` explicitamente volta ao relógio real."""
    peak_kw: float | None = None
    sim_hour: float | None = None
