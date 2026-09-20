"""
ChargeGrid Intelligence — RemoteController
--------------------------------------------
Substitui a antiga classe local `DemandController` (que morava em
demand_controller.py, agora removido deste projeto). Em vez de guardar o
estado elétrico em memória e calcular a redistribuição aqui, esta classe
é um CLIENTE HTTP do backend real (o mesmo que serve o ESP32):

    frontend (este projeto, HTML/Jinja2/HTMX/WS)  --HTTP-->  backend (FastAPI puro, a "verdade")

Por que existe (em vez de trocar `requests.get(...)` direto em cada rota
do main.py): o `main.py` original foi escrito inteiro contra a interface
do DemandController antigo (`controller.chargers[id].is_occupied`,
`controller.active_sessions`, `controller.vehicle_connect(...)`, etc.).
Refazer TODAS as ~40 rotas para falar HTTP diretamente seria um projeto à
parte. Em vez disso, esta classe IMITA essa mesma interface por fora —
troca só a fonte dos dados por dentro — então o resto do main.py muda
pouquíssimo (principalmente: adicionar `await` nas chamadas que agora são
requisições de rede).

Estratégia de cache: os métodos de LEITURA (chargers, active_sessions,
available_kw, etc.) são síncronos e leem de um cache local
(`self._snapshot`), igual ao DemandController original. Esse cache é
atualizado por `refresh()`, chamado (a) uma vez na inicialização e
(b) a cada 1s pelo `ticker_loop()` do main.py — o mesmo loop que antes
fazia `session.update_energy()` manualmente agora só busca o estado mais
recente do backend real (que já faz esse cálculo sozinho, ver
core/demand_controller.py -> tick_energy()). Os métodos de ESCRITA
(vehicle_connect/vehicle_disconnect) são assíncronos de verdade — fazem
a requisição HTTP e, se der certo, atualizam o cache antes de devolver.
"""

from __future__ import annotations

from datetime import datetime

import httpx


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class _SessionView:
    """Imita ChargingSession o suficiente para o main.py/chatbot.py não notarem a troca."""

    def __init__(self, data: dict):
        self.charger_id = data.get("charger_id") or data.get("id")
        self.vehicle_id = data["vehicle_id"]
        self.energy_consumed_kwh = data.get("energy_kwh") or 0.0
        self.allocated_kw = data.get("allocated_kw", 0.0)
        self.started_at = _parse_iso(data.get("started_at"))
        self._duration_min = data.get("duration_min") or 0.0

    def duration_minutes(self) -> float:
        # Sessões ATIVAS: duration_min já vem calculado no snapshot (fresco
        # a cada refresh(), no máximo ~1s desatualizado). Sessões
        # concluídas: é o valor final, fixo.
        return self._duration_min


class _ChargerView:
    """Imita Charger o suficiente para o main.py/chatbot.py não notarem a troca."""

    def __init__(self, data: dict):
        self.charger_id = data["id"]
        self.max_kw = data["max_kw"]
        self.location = data["location"]
        self.status = data["status"]
        self.session = _SessionView(data) if data["status"] == "OCUPADO" else None

    @property
    def is_occupied(self) -> bool:
        return self.status == "OCUPADO"

    @property
    def current_kw(self) -> float:
        return self.session.allocated_kw if self.session else 0.0


class RemoteController:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=6.0)

        self._snapshot: dict = {
            "site": "", "contracted_limit_kw": 0.0, "usable_limit_kw": 0.0, "usable_limit_pct": 0.0,
            "total_allocated_kw": 0.0, "available_kw": 0.0, "utilization_pct": 0.0,
            "active_sessions": 0, "chargers": [], "events": [],
        }
        self._completed_raw: list[dict] = []

        # Eventos de UI/demo (pagamento aprovado, flag de manutenção, Pix
        # criado...) — não existem no backend real, que só sabe de eventos
        # elétricos (conectar/desconectar/redistribuir). Ficam só aqui.
        self._local_events: list[dict] = []

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Sincronização com o backend real
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        resp = await self._client.get("/api/chargers")
        resp.raise_for_status()
        self._snapshot = resp.json()

    async def refresh_sessions(self) -> None:
        resp = await self._client.get("/api/sessions")
        resp.raise_for_status()
        self._completed_raw = resp.json()

    # ------------------------------------------------------------------
    # Leituras — síncronas, servidas do cache (self._snapshot)
    # ------------------------------------------------------------------

    def _state_snapshot(self) -> dict:
        snap = dict(self._snapshot)
        snap["events"] = (self._snapshot.get("events", []) + self._local_events)[-5:]
        return snap

    @property
    def chargers(self) -> dict[str, _ChargerView]:
        return {c["id"]: _ChargerView(c) for c in self._snapshot.get("chargers", [])}

    @property
    def active_sessions(self) -> list[_SessionView]:
        return [c.session for c in self.chargers.values() if c.session]

    @property
    def completed_sessions(self) -> list[_SessionView]:
        return [_SessionView(s) for s in self._completed_raw]

    @property
    def available_kw(self) -> float:
        return self._snapshot.get("available_kw", 0.0)

    @property
    def usable_limit_kw(self) -> float:
        return self._snapshot.get("usable_limit_kw", 0.0)

    @property
    def usable_limit_pct(self) -> float:
        return self._snapshot.get("usable_limit_pct", 0.0)

    @property
    def total_allocated_kw(self) -> float:
        return self._snapshot.get("total_allocated_kw", 0.0)

    @property
    def utilization_pct(self) -> float:
        return self._snapshot.get("utilization_pct", 0.0)

    @property
    def events(self) -> list[dict]:
        return self._snapshot.get("events", []) + self._local_events

    # ------------------------------------------------------------------
    # Ações — assíncronas de verdade, mudam o estado no backend real
    # ------------------------------------------------------------------

    async def vehicle_connect(self, charger_id: str, vehicle_id: str) -> dict:
        resp = await self._client.post(
            f"/api/chargers/{charger_id}/connect", json={"vehicle_id": vehicle_id}
        )
        if resp.status_code >= 400:
            return {"error": resp.json().get("detail", "Erro ao conectar.")}
        await self.refresh()
        return self._state_snapshot()

    async def vehicle_disconnect(self, charger_id: str) -> dict:
        resp = await self._client.post(f"/api/chargers/{charger_id}/disconnect")
        if resp.status_code >= 400:
            return {"error": resp.json().get("detail", "Erro ao desconectar.")}
        await self.refresh()
        await self.refresh_sessions()
        return self._state_snapshot()

    async def import_sessions(self, sessions: list[dict]) -> dict:
        """Importa sessões JÁ CONCLUÍDAS no histórico do backend real
        (POST /api/sessions/import — idempotente). Usado para popular a demo
        com as recargas anteriores dos clientes fictícios (ver demo_seed.py).
        Devolve {"imported", "skipped"} ou {"error": mensagem}."""
        try:
            resp = await self._client.post("/api/sessions/import", json={"sessions": sessions})
        except httpx.HTTPError:
            return {"error": "Não foi possível falar com o backend agora."}
        if resp.status_code >= 400:
            return {"error": resp.json().get("detail", "Erro ao importar sessões.")}
        await self.refresh_sessions()
        return resp.json()

    async def get_settings(self) -> dict | None:
        """Demanda contratada + limite de uso (%) atuais, direto do backend real
        (inclui os limites min/max aceitos, pra a tela validar com o mesmo critério).
        Devolve None se o backend não responder — a tela mostra o que tem em cache."""
        try:
            resp = await self._client.get("/api/settings")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            return None

    async def update_settings(self, contracted_limit_kw: float | None = None,
                              usable_limit_pct: float | None = None) -> dict:
        """Altera demanda contratada e/ou limite de uso no backend real, que
        redistribui a potência dos carregadores na hora. Devolve as configurações
        novas, ou {"error": mensagem} (mesmo padrão de vehicle_connect())."""
        body = {"contracted_limit_kw": contracted_limit_kw, "usable_limit_pct": usable_limit_pct}
        try:
            resp = await self._client.put("/api/settings", json=body)
        except httpx.HTTPError:
            return {"error": "Não foi possível falar com o backend agora — tente novamente."}
        if resp.status_code >= 400:
            detail = resp.json().get("detail", "Erro ao salvar.") if resp.headers.get("content-type", "").startswith("application/json") else "Erro ao salvar."
            if isinstance(detail, list):  # 422 de validação do pydantic (ex.: texto no lugar de número)
                detail = "Valor inválido."
            return {"error": detail}
        await self.refresh()
        return resp.json()

    async def get_predictions(self, hours: int = 24) -> list[dict] | None:
        """
        Busca a previsão de demanda (pilar de IA) direto do backend real.
        Devolve None (em vez de lançar exceção) se o modelo ainda não foi
        treinado (HTTP 503) — assim o dashboard mostra uma mensagem
        amigável em vez de quebrar a página inteira.
        """
        resp = await self._client.get(f"/api/predictions/proximas-horas?n={hours}")
        if resp.status_code == 503:
            return None
        resp.raise_for_status()
        return resp.json()["forecast"]

    # ------------------------------------------------------------------
    # Eventos de UI/demo — nunca tocam o backend real
    # ------------------------------------------------------------------

    def _log_event(self, event_type: str, source: str, message: str) -> None:
        self._local_events.append({
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "type": event_type,
            "source": source,
            "message": message,
        })
        self._local_events[:] = self._local_events[-100:]
