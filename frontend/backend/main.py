"""
ChargeGrid Intelligence — FastAPI app (frontend / BFF)
---------------------------------------------------------
Serve o Dashboard de Gestão e o App do Cliente, ambos sincronizados em
tempo real via WebSocket. Este processo NÃO tem mais lógica de negócio
própria — todo o estado elétrico real (carregadores, sessões, redistri-
buição de potência) vive no backend principal (chargegrid_backend/),
acessado via HTTP através de `remote_controller.RemoteController` (ver
esse arquivo para o porquê do desenho). HTMX cuida das interações de
clique/formulário; o JS próprio (static/app.js) cuida só da conexão
WebSocket e das animações SVG do diagrama unifilar / anel de energia
"""

import asyncio
import base64
import io
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import qrcode
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from remote_controller import RemoteController
import chatbot
import client_store
import company_store
import demo_seed
import payments
import security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chargegrid.main")

# ----------------------------------------------------------------------
# Configuração do site / tarifa
# ----------------------------------------------------------------------

SITE_NAME = "Shopping Paulista — Estacionamento"
# A TARIFA não mora mais aqui: o preço do kWh é dinâmico e calculado pelo backend
# (core/tariff.py), guiado pela previsão de demanda da IA. Cada sessão carrega o
# preço travado na conexão (cost_rs). Ver current_price() abaixo.

# URL do backend real (o mesmo que o ESP32 chama). Em dev, os dois rodam
# na mesma máquina em portas diferentes; em produção, aponta pra URL do
# Render/Railway via variável de ambiente.
BACKEND_API_URL = os.environ.get("CHARGEGRID_API_URL", "http://127.0.0.1:8000")

TICK_INTERVAL_S = 1.0
HISTORY_PAGE_LIMIT = 300   # linhas da tela Histórico do dashboard (as mais recentes)

# Modo demonstração (ligado por padrão): cadastra os clientes fictícios de
# demo_seed.py, importa o histórico de recargas deles e habilita as rotas
# /demo/* que os scripts de bastidor usam pra iniciar/encerrar recargas ao
# vivo pela MESMA camada do app do cliente (sem o pagamento). DEMO_MODE=0
# desliga tudo isso (ex.: para rodar limpo, sem dados fictícios).
DEMO_MODE = os.environ.get("DEMO_MODE", "1") != "0"

# ----------------------------------------------------------------------
# Estado global (demo single-process, em memória) — agora é só o CACHE
# do estado que vive de verdade no backend real (ver remote_controller.py)
# ----------------------------------------------------------------------

controller = RemoteController(BACKEND_API_URL, api_key=security.api_key())


def current_price() -> float:
    """Preço atual do kWh (R$) — lido do backend (tarifa dinâmica), nunca calculado aqui."""
    return float(controller.tariff.get("price_rs_per_kwh") or 0.0)


class ConnectionManager:
    """Gerencia os WebSockets ativos e faz broadcast do estado."""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# ----------------------------------------------------------------------
# Camada de demo — SEM alterar demand_controller.py
# ----------------------------------------------------------------------
# payment_status e sim_flags vivem aqui, fora do Charger/ChargingSession.
# O DemandController continua sabendo só de OCUPADO/LIVRE; isto é uma
# camada visual/demo por cima, mapeando charger_id -> estado mockado.
#
#   payment_status: "pago" | "pendente"           (mock do item P2-13)
#   sim_flags:       None | "manutencao" | "erro"  (mock do item P2-12,
#                     nunca entra no algoritmo de _redistribute_power)

payment_status: dict[str, str] = {}
sim_flags: dict[str, str | None] = {}

# ----------------------------------------------------------------------
# Camada do fluxo do cliente (login → carregar → pagar) — também fora do
# DemandController, pelo mesmo motivo: são dados de UI/demo (meta
# escolhida, qual cliente está em qual carregador), não estado elétrico.
#
#   charging_goals:      charger_id -> {"goal_type": "tempo"|"energia",
#                         "target_min", "target_kwh", "cpf"} da sessão ativa
#                         — target_min/target_kwh é o valor ESCOLHIDO pelo
#                         cliente no modo correspondente (exato, não
#                         estimado); o outro campo fica None. O ticker usa
#                         isso pra saber quando encerrar sozinho (P0-5).
#   client_active_charger: cpf -> charger_id (para achar "minha recarga")
#   last_receipt:         cpf -> resumo da última recarga concluída (para a
#                          tela de conclusão sobreviver a um F5) — inclui
#                          "completed_reason": "manual" | "meta_atingida"
# ----------------------------------------------------------------------

charging_goals: dict[str, dict] = {}
client_active_charger: dict[str, str] = {}
last_receipt: dict[str, dict] = {}

# ----------------------------------------------------------------------
# Camada de pagamento Pix (PAYMENT_MODE=mercadopago) — cpf -> cobrança
# pendente. Vive fora do DemandController pelo mesmo motivo das outras
# camadas de demo acima: vehicle_connect() só é chamado DEPOIS que o
# status do pagamento vier aprovado (ver /cliente/carregar/pix/status),
# então enquanto o Pix está pendente o carregador nem está OCUPADO
# ainda — não daria pra guardar isso em charging_goals/client_active_charger.
#
#   pix_pending: cpf -> {charger_id, placa, goal_type, target_min,
#                        target_kwh, order_id, qr_code, qr_code_base64,
#                        valor_rs, created_at}
# ----------------------------------------------------------------------

pix_pending: dict[str, dict] = {}

PIX_TIMEOUT_S = 5 * 60  # item P0-8: totem volta sozinho se o Pix não confirmar em ~5 min

# ----------------------------------------------------------------------
# P1-2: token de acesso temporário do QR code da tela de confirmação do
# totem — NÃO é a sessão de login completa do cliente. Token de USO
# ÚNICO (removido do dict assim que /cliente/sessao/<token> é acessado,
# mesmo que já esteja expirado) e com expiração curta. Atalho de
# conveniência pra demo, não um mecanismo de segurança real — mas ainda
# assim não fica reutilizável nem sem prazo.
#
#   session_qr_tokens: token -> {"cpf", "expires_at"}
# ----------------------------------------------------------------------

session_qr_tokens: dict[str, dict] = {}

SESSION_QR_TTL_S = 10 * 60  # 10 minutos OU até ser usado uma vez, o que vier primeiro


def utilization_level(pct: float) -> str:
    """'ok' | 'warning' (>=90%) | 'critical' (>100%)"""
    if pct > 100:
        return "critical"
    if pct >= 90:
        return "warning"
    return "ok"


# ----------------------------------------------------------------------
# Faturamento / curva de lucro
# ----------------------------------------------------------------------

def format_brl(value: float) -> str:
    """Formata um valor como moeda no padrão brasileiro: 'R$ 1.234,56'."""
    s = f"{value:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {s}"


def billing_snapshot() -> dict:
    """Números de HOJE do painel de faturamento (o histórico guarda vários dias,
    então só entram as sessões que terminaram hoje + as em andamento)."""
    active = controller.active_sessions
    completed = controller.today_completed_sessions
    # Receita = soma do custo de cada sessão, com o preço travado NA CONEXÃO dela.
    completed_rs = sum(s.cost_rs for s in completed)
    revenue_rs = completed_rs + sum(s.cost_rs for s in active)
    total_kwh = sum(s.energy_consumed_kwh for s in completed) + sum(s.energy_consumed_kwh for s in active)

    # Ticket médio e duração média — só sobre as sessões já finalizadas (as ativas
    # ainda estão em andamento e distorceriam a métrica).
    n_completed = len(completed)
    avg_ticket_rs = (completed_rs / n_completed) if n_completed else None
    avg_duration_min = (sum(s.duration_minutes() for s in completed) / n_completed) if n_completed else None

    return {
        "tariff_rs_per_kwh": current_price(),
        "kwh_today": round(total_kwh, 3),
        "revenue_today_rs": round(revenue_rs, 2),
        "sessions_today": len(active) + n_completed,
        "completed_sessions_count": n_completed,
        "avg_ticket_rs": round(avg_ticket_rs, 2) if avg_ticket_rs is not None else None,
        "avg_duration_min": round(avg_duration_min, 1) if avg_duration_min is not None else None,
    }


_WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
_PERIODS = {"dia": "day", "semana": "week", "mes": "month"}


def _brl_compact(value: float) -> str:
    """R$ 600 / R$ 1,2 mil — para os rótulos do eixo do gráfico."""
    if value >= 1000:
        return "R$ " + f"{value / 1000:.1f}".replace(".", ",").replace(",0", "") + " mil"
    return f"R$ {value:.0f}"


def _nice_ceiling(value: float) -> float:
    """Menor 1, 2, 2,5, 5 ou 10 x 10^n que comporta `value` (teto "redondo" do eixo)."""
    if value <= 0:
        return 100.0
    import math
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def revenue_chart_view(series: dict, periodo: str) -> dict:
    """Só apresentação: transforma a série do backend (GET /api/billing/series) em
    barras com altura, rótulos e textos do tooltip. Nenhum cálculo de negócio."""
    buckets = series["buckets"]
    current = series["current_index"]
    ceiling = _nice_ceiling(max((b["revenue_rs"] for b in buckets), default=0.0))
    bars = []
    for i, b in enumerate(buckets):
        start = datetime.fromisoformat(b["start"])
        if periodo == "dia":
            label = f"{start.hour}h" if i % 3 == 0 else ""
            head = f"{start.hour}h–{(start.hour + 1) % 24}h"
        else:
            wd = _WEEKDAYS_PT[start.weekday()]
            head = f"{wd}, {start:%d/%m}" + (" (hoje)" if i == current else "")
            if periodo == "semana":
                label = f"{wd} {start:%d}"
            else:
                label = f"{start:%d}" if (i % 5 == 0 or i == len(buckets) - 1) else ""
        detail = (f"{b['sessions']} sessão(ões) · {b['kwh']:.1f} kWh" if b["sessions"]
                  else "nenhuma sessão neste período")
        bars.append({
            "height": max(2.0, round(b["revenue_rs"] / ceiling * 100, 1)) if b["revenue_rs"] > 0 else 2.0,
            "label": label, "head": head, "main": format_brl(b["revenue_rs"]), "detail": detail,
            "is_current": i == current,
            "is_future": periodo == "dia" and i > current,
        })
    delta = series["delta_pct"]
    if delta is None:
        delta_text, delta_tone = f"sem dados de {series['compare_label']} para comparar", "neutral"
    else:
        sign = "+" if delta >= 0 else "−"
        delta_text = f"{sign}{abs(delta):.1f}% vs {series['compare_label']}".replace(".", ",")
        delta_tone = "ok" if delta >= 0 else "warn"
    titles = {"dia": ("por hora", "hoje"), "semana": ("por dia", "últimos 7 dias"), "mes": ("por dia", "últimos 30 dias")}
    unit_word = "hora" if periodo == "dia" else "dia"
    return {
        "periodo": periodo, "bars": bars,
        "title": f"Faturamento {titles[periodo][0]}", "range_label": titles[periodo][1],
        "total": series["total_rs"], "sessions": series["sessions"], "kwh": series["total_kwh"],
        "delta_text": delta_text, "delta_tone": delta_tone,
        "ticks": [_brl_compact(ceiling), _brl_compact(ceiling / 2), "R$ 0"],
        "caption": f"Cada barra é o total faturado em cada {unit_word} (sessões concluídas e em andamento). "
                   f"A barra vermelha é {'a hora' if periodo == 'dia' else 'o dia'} de hoje.",
    }


def _plate_owners() -> dict[str, str]:
    """placa -> nome do cliente dono (só placas que já passaram por uma conta)."""
    return {plate: account["nome"] for account in client_store.accounts.values()
            for plate in account.get("veiculos", [])}


def completed_sessions_payload() -> list[dict]:
    owners = _plate_owners()
    return [
        {
            "charger_id": s.charger_id,
            "vehicle_id": s.vehicle_id,
            "client_name": owners.get(s.vehicle_id),
            "started_at": s.started_at.isoformat(),
            "started_at_date": s.started_at.strftime("%d/%m/%Y"),
            "duration_min": round(s.duration_minutes(), 1),
            "energy_kwh": round(s.energy_consumed_kwh, 3),
            "revenue_rs": round(s.cost_rs, 2),
            "tariff_rs_per_kwh": s.tariff_rs_per_kwh,
            "green_kwh": round(s.green_kwh, 3),
        }
        for s in controller.completed_sessions
    ]


def dashboard_clients_payload() -> list[dict]:
    """Lista de contas cadastradas pro item 'Clientes' do dashboard (P0-10)
    — reaproveita client_store.py e o histórico do backend, sem nenhum estado novo."""
    completed = completed_sessions_payload()
    rows = []
    for account in client_store.accounts.values():
        plates = set(account.get("veiculos", []))
        mine = [s for s in completed if s["vehicle_id"] in plates]
        rows.append({
            "nome": account["nome"],
            "cpf": account["cpf"],
            "email": account["email"],
            "veiculos": account.get("veiculos_cadastrados", []),
            "n_sessions": len(mine),
            "energy_kwh": round(sum(s["energy_kwh"] for s in mine), 3),
            "spent_rs": round(sum(s["revenue_rs"] for s in mine), 2),
            "charging_at": client_active_charger.get(account["cpf"]),  # vaga em que está carregando agora (ou None)
        })
    return rows


def enrich_charger(c: dict) -> dict:
    """Anexa as flags de demo (fora do DemandController) ao dict de um carregador."""
    c = dict(c)
    c["payment_status"] = payment_status.get(c["id"], "pago")
    c["sim_flag"] = sim_flags.get(c["id"])
    # Quem está nesta vaga: o cliente que iniciou a recarga pelo app (charging_goals)
    # ou, na falta disso, o dono da placa (ex.: veículo conectado direto pelo operador).
    goal = charging_goals.get(c["id"])
    owner = client_store.accounts.get(goal["cpf"]) if goal else None
    c["client_name"] = owner["nome"] if owner else (_plate_owners().get(c.get("vehicle_id")) if c.get("vehicle_id") else None)
    return c


def build_payload() -> dict:
    state = controller._state_snapshot()
    state["clock"] = datetime.now().strftime("%H:%M:%S")
    state["billing"] = billing_snapshot()
    state["utilization_level"] = utilization_level(state["utilization_pct"])
    state["chargers"] = [enrich_charger(c) for c in state["chargers"]]
    return state


def generate_qr_base64(data: str) -> str:
    """
    Gera o PNG de um QR code no SERVIDOR (biblioteca `qrcode`, sem API
    externa) e devolve como base64, pra embutir direto no HTML com
    `data:image/png;base64,...` — mesmo padrão já usado pro QR do Pix
    (ver payments.py / carregar_pix.html). Evita depender de rede externa
    pra um elemento visual, como já aconteceu antes com um ícone via CDN.
    """
    img = qrcode.make(data, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _issue_session_qr_url(request: Request, cpf: str) -> str:
    """
    P1-2: emite um token de uso único (curto prazo) e devolve a URL
    completa que o QR code vai codificar (`/cliente/sessao/<token>`).
    Faz uma limpeza preguiçosa de tokens expirados a cada emissão — o
    dict é pequeno, não precisa de um job de limpeza separado.
    """
    now = datetime.now()
    for existing_token, entry in list(session_qr_tokens.items()):
        if entry["expires_at"] < now:
            session_qr_tokens.pop(existing_token, None)

    token = secrets.token_urlsafe(24)
    session_qr_tokens[token] = {"cpf": cpf, "expires_at": now + timedelta(seconds=SESSION_QR_TTL_S)}
    return f"{request.base_url}cliente/sessao/{token}"


# ----------------------------------------------------------------------
# Estimativa de recarga (App do Cliente) — NÃO toca no DemandController
# ----------------------------------------------------------------------

def estimate_charge(charger_id: str, goal_type: str, valor: float, tariff: float | None = None) -> dict:
    """
    Estimativa simples de tempo/custo para uma recarga que AINDA NÃO
    começou, baseada na condição ATUAL da rede (kW ainda disponíveis
    agora). NÃO é um modelo de previsão de horário de pico nem simula a
    redistribuição real que aconteceria ao conectar — é só uma projeção
    ingênua assumindo potência constante, para dar uma noção ao cliente
    antes de pagar. A tela deixa isso explícito para não parecer uma
    previsão de verdade.

    Espelha o mesmo cálculo nas duas direções (P0-2):
      - goal_type="tempo": `valor` = minutos desejados → projeta energia/custo.
      - goal_type="energia": `valor` = kWh desejados → projeta tempo/custo
        (comportamento original, inalterado).

    IMPORTANTE: o valor de retorno aqui é só para exibição ("energia
    estimada", "tempo estimado"). A META de verdade que o ticker usa pra
    encerrar automaticamente (ver charging_goals) é sempre o `valor`
    exato escolhido pelo cliente no modo escolhido — nunca a projeção do
    outro campo, que pode mudar se a potência disponível mudar.
    """
    price = tariff if tariff and tariff > 0 else current_price()
    charger = controller.chargers[charger_id]
    estimated_power_kw = round(min(charger.max_kw, max(0.0, controller.available_kw)), 2)

    if goal_type == "tempo":
        target_min = round(valor, 1)
        target_kwh = round(estimated_power_kw * target_min / 60, 2) if estimated_power_kw > 0 else None
        estimated_time_min = target_min
    else:
        target_kwh = round(valor, 2)
        estimated_time_min = round((target_kwh / estimated_power_kw) * 60, 1) if estimated_power_kw > 0 else None
        target_min = estimated_time_min

    estimated_cost_rs = round(target_kwh * price, 2) if target_kwh is not None else None

    return {
        "charger_id": charger_id,
        "goal_type": goal_type,
        "target_kwh": target_kwh,
        "target_min": target_min,
        "estimated_power_kw": estimated_power_kw,
        "estimated_time_min": estimated_time_min,
        "estimated_cost_rs": estimated_cost_rs,
        "tariff_rs_per_kwh": price,
    }


def _build_receipt(session, charger_id: str, location: str, reason: str) -> dict:
    """Recibo da recarga. Prefere a sessão JÁ ARQUIVADA pelo backend (valores finais,
    iguais aos do histórico); cai para a última leitura em cache se não a achar."""
    done = next((s for s in reversed(controller.completed_sessions)
                 if s.charger_id == charger_id and s.vehicle_id == session.vehicle_id), session)
    return {
        "vehicle_id": done.vehicle_id,
        "charger_id": charger_id,
        "location": location,
        "energy_kwh": round(done.energy_consumed_kwh, 3),
        "duration_min": round(done.duration_minutes(), 1),
        "revenue_rs": round(done.cost_rs, 2),
        "tariff_rs_per_kwh": done.tariff_rs_per_kwh,
        "green_kwh": round(done.green_kwh, 3),
        "completed_reason": reason,
    }


async def _check_goals_and_autodisconnect() -> None:
    """
    P0-5: a cada tick, verifica se alguma sessão ativa atingiu a meta
    contratada (tempo OU energia, conforme escolhido em /cliente/carregar)
    e, se sim, encerra automaticamente — reaproveita vehicle_disconnect()
    do DemandController (INTOCÁVEL), nunca duplica a lógica de
    redistribuição.

    Idempotência: se o cliente clicou "encerrar" manualmente quase ao
    mesmo tempo, o carregador já não estará mais `is_occupied` (ou
    charging_goals[charger_id] já não existe mais) quando este loop
    rodar de novo — o `continue` abaixo ignora esses casos em silêncio,
    sem chamar vehicle_disconnect() duas vezes.
    """
    for charger_id, goal in list(charging_goals.items()):
        charger = controller.chargers.get(charger_id)
        if not charger or not charger.is_occupied:
            continue
        session = charger.session

        if goal["goal_type"] == "tempo":
            reached = goal.get("target_min") is not None and session.duration_minutes() >= goal["target_min"]
        else:
            reached = goal.get("target_kwh") is not None and session.energy_consumed_kwh >= goal["target_kwh"]
        if not reached:
            continue

        cpf = goal["cpf"]
        location = charger.location
        result = await controller.vehicle_disconnect(charger_id)
        if isinstance(result, dict) and "error" in result:
            continue  # já foi encerrada por outro caminho nesse meio-tempo — ignora

        controller._log_event(
            "CLIENT_GOAL_REACHED", charger_id,
            f"Meta de {goal['goal_type']} atingida — recarga encerrada automaticamente",
        )
        charging_goals.pop(charger_id, None)
        client_active_charger.pop(cpf, None)
        last_receipt[cpf] = _build_receipt(session, charger_id, location, "meta_atingida")


# ----------------------------------------------------------------------
# Ticker: acumula energia das sessões ativas, checa metas e transmite o
# estado a cada 1s
# ----------------------------------------------------------------------

async def ticker_loop() -> None:
    """
    Antes: acumulava energia manualmente em cada sessão ativa (o backend
    real agora faz isso sozinho, ver core/demand_controller.py ->
    tick_energy()). Hoje só busca o estado mais recente do backend a cada
    1s — é isso que faz os números da tela "andarem sozinhos".
    """
    tick = 0
    while True:
        await asyncio.sleep(TICK_INTERVAL_S)
        tick += 1
        try:
            await controller.refresh()
            if DEMO_MODE and not _demo_seeded and tick % 5 == 0:   # backend voltou depois de uma partida sem ele
                await controller.refresh_sessions()
                await _try_seed_demo()
            if tick % 30 == 0:               # curva de preço muda devagar: renova a cada ~30s
                await controller.refresh_tariff()
            await _check_goals_and_autodisconnect()
            if controller.active_sessions or manager.active:
                await manager.broadcast(build_payload())
        except Exception as exc:  # backend reiniciando/lento: segue tentando, não mata o loop
            logger.warning("ticker: falha ao sincronizar com o backend (%s) — tentando de novo.", exc)


# ----------------------------------------------------------------------
# App / lifespan
# ----------------------------------------------------------------------

_demo_seeded = False


async def _try_seed_demo() -> None:
    """Roda o seed da demo; se o backend não responder, deixa para o ticker repetir."""
    global _demo_seeded
    try:
        _demo_seeded = await _seed_demo()
    except Exception as exc:
        logger.warning("Demo: seed adiado — backend indisponível (%s).", exc)


async def _seed_demo() -> bool:
    """Cadastra os clientes fictícios e importa as recargas anteriores deles
    no histórico do backend real. Só importa o histórico se ele ainda não
    tiver sessões dessas placas (senão, reiniciar SÓ o frontend duplicaria).
    Devolve False se o histórico não pôde ser importado (backend fora do ar) —
    o ticker tenta de novo depois."""
    created = demo_seed.seed_clients()
    plates = demo_seed.demo_plates()
    if any(s.vehicle_id in plates for s in controller.completed_sessions):
        logger.info("Demo: histórico dos clientes fictícios já presente — nada a importar.")
    else:
        result = await controller.import_sessions(demo_seed.history_items() + demo_seed.past_days_items())
        if "error" in result:
            logger.warning("Demo: não consegui importar o histórico (%s) — tento de novo em instantes.", result["error"])
            return False
        else:
            logger.info("Demo: %d sessão(ões) anterior(es) importada(s) para o histórico (hoje + %d dias).",
                        result["imported"], demo_seed.PAST_DAYS)
            # O histórico novo vira dado de treino: a previsão (e a tarifa dinâmica que depende
            # dela) passa a refletir também esses dias.
            metrics = await controller.retrain_model()
            if "error" in metrics:
                logger.warning("Demo: histórico importado, mas o re-treino da IA falhou (%s).", metrics["error"])
            else:
                logger.info("Demo: IA re-treinada com %d horas do histórico — MAE %.2f kW, R² %.3f.",
                            metrics["real_samples"], metrics["mae_kw"], metrics["r2"])
    logger.info("Demo: %d cliente(s) fictício(s) cadastrado(s) — login com CPF + senha '%s':", created, demo_seed.DEMO_PASSWORD)
    for c in demo_seed.DEMO_CLIENTS:
        logger.info("   %s  %-18s %s", c["cpf"], c["nome"], ", ".join(v["placa"] for v in c["veiculos"]))
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Os 12 carregadores já existem no backend real (core/state.py de
    # lá) — aqui só busca o estado inicial e prepara as camadas de
    # demo/UI (payment_status, sim_flags) que vivem só neste projeto.
    # O frontend precisa subir mesmo com o backend fora do ar (ordem de inicialização,
    # reinício, queda): o ticker segue tentando e a tela se completa quando ele volta.
    try:
        await controller.refresh()
        await controller.refresh_sessions()
        await controller.refresh_tariff()
    except Exception as exc:
        logger.warning("Backend %s indisponível na inicialização (%s) — o ticker tenta de novo a cada 1s.",
                       BACKEND_API_URL, exc)
    for charger_id in controller.chargers:
        payment_status[charger_id] = "pago"
        sim_flags[charger_id] = None
    if DEMO_MODE:
        await _try_seed_demo()
    task = asyncio.create_task(ticker_loop())
    logger.info("ChargeGrid Intelligence (frontend) iniciado — conectado a %s | %d carregadores",
                BACKEND_API_URL, len(controller.chargers))
    yield
    task.cancel()
    await controller.aclose()


app = FastAPI(title="ChargeGrid Intelligence", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters["brl"] = format_brl


# ----------------------------------------------------------------------
# Rotas — páginas
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Dashboard de Gestão — casca de navegação (P0-10): Visão Geral (resumo
# de alto nível) + Carregadores (grid/tabela detalhada, era o antigo "/")
# + Clientes/Histórico/Relatórios/Configurações, todos reaproveitando o
# estado que já existe (controller, client_store, billing_snapshot) —
# nenhum motor de analytics novo.
# ----------------------------------------------------------------------

def _current_company(request: Request) -> dict | None:
    token = request.cookies.get(company_store.SESSION_COOKIE)
    return company_store.get_company_from_token(token)


def _require_company(request: Request) -> RedirectResponse | None:
    """Guard usado no início de toda rota /dashboard/*. Devolve o
    redirect pro login de empresa se não houver sessão válida, ou None
    se puder seguir — quem chama decide (`if redirect: return redirect`),
    já que aqui não temos Depends() no resto do arquivo."""
    if _current_company(request) is None:
        return RedirectResponse("/empresa/login", status_code=303)
    return None


def _require_operator(request: Request) -> None:
    """Guard das rotas de AÇÃO/JSON usadas pelo dashboard (/connect, /disconnect,
    /sessions, /chat, /charger/*/demo/*): sem login de empresa, 401. Diferente de
    _require_company, não redireciona — essas rotas são chamadas por HTMX/fetch."""
    if _current_company(request) is None:
        raise HTTPException(status_code=401, detail="Login de operador necessário.")


def _is_loopback(request: Request) -> bool:
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1", "localhost")


def _require_operator_or_key(request: Request) -> None:
    """Guard das rotas /demo/* (scripts de bastidor, sem cookie): aceita (1) login de
    operador, (2) cabeçalho X-API-Key com a chave configurada ou (3) — só quando NENHUMA
    chave está configurada (dev local) — chamadas vindas da própria máquina."""
    if _current_company(request) is not None:
        return
    if security.key_matches(request.headers.get("x-api-key")):
        return
    if not security.api_key() and _is_loopback(request):
        return
    raise HTTPException(status_code=401, detail="Informe login de operador ou X-API-Key.")


@app.get("/", response_class=HTMLResponse)
async def entrada(request: Request):
    """Landing pública: escolha entre App do Cliente (motorista) e
    Dashboard de Gestão (empresa) — antes disto o dashboard vivia
    direto na raiz, sem login nenhum."""
    return templates.TemplateResponse("entrar.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_visao_geral(request: Request):
    if (redirect := _require_company(request)) is not None:
        return redirect
    payload = build_payload()
    forecast = await controller.get_predictions(hours=24)
    return templates.TemplateResponse(
        "dashboard_gestao.html",
        {"request": request, "state": payload, "active_nav": "visao",
         "forecast": forecast, "tariff_forecast": controller.tariff_forecast, "company": _current_company(request)},
    )


@app.get("/dashboard/carregadores", response_class=HTMLResponse)
async def dashboard_carregadores(request: Request):
    if (redirect := _require_company(request)) is not None:
        return redirect
    payload = build_payload()
    return templates.TemplateResponse(
        "dashboard_carregadores.html",
        {"request": request, "state": payload, "active_nav": "carregadores",
         "company": _current_company(request)},
    )


@app.get("/dashboard/clientes", response_class=HTMLResponse)
async def dashboard_clientes(request: Request):
    if (redirect := _require_company(request)) is not None:
        return redirect
    return templates.TemplateResponse(
        "dashboard_clientes.html",
        {"request": request, "clients": dashboard_clients_payload(), "active_nav": "clientes",
         "company": _current_company(request)},
    )


@app.get("/dashboard/historico", response_class=HTMLResponse)
async def dashboard_historico(request: Request):
    if (redirect := _require_company(request)) is not None:
        return redirect
    sessions = completed_sessions_payload()
    sessions.reverse()
    total = len(sessions)
    sessions = sessions[:HISTORY_PAGE_LIMIT]     # o histórico guarda semanas; a tela mostra as mais recentes
    return templates.TemplateResponse(
        "dashboard_historico.html",
        {"request": request, "sessions": sessions, "history_total": total, "history_limit": HISTORY_PAGE_LIMIT,
         "active_nav": "historico",
         "company": _current_company(request)},
    )


@app.get("/dashboard/faturamento/grafico", response_class=HTMLResponse)
async def dashboard_faturamento_grafico(request: Request, periodo: str = "dia"):
    """Gráfico de faturamento (Dia | Semana | Mês). Devolve só o bloco do gráfico —
    as abas e o auto-refresh (HTMX) trocam esse bloco inteiro."""
    _require_operator(request)
    if periodo not in _PERIODS:
        raise HTTPException(status_code=400, detail="Período inválido (use dia, semana ou mes).")
    series = await controller.get_billing_series(_PERIODS[periodo])
    chart = revenue_chart_view(series, periodo) if series else None
    return templates.TemplateResponse(
        "partials/revenue_chart.html", {"request": request, "chart": chart, "periodo": periodo})


@app.get("/dashboard/relatorios", response_class=HTMLResponse)
async def dashboard_relatorios(request: Request):
    if (redirect := _require_company(request)) is not None:
        return redirect
    forecast = await controller.get_predictions(hours=24)
    return templates.TemplateResponse(
        "dashboard_relatorios.html",
        {"request": request, "billing": billing_snapshot(), "forecast": forecast, "active_nav": "relatorios",
         "state": build_payload(), "tariff_forecast": controller.tariff_forecast,
         "company": _current_company(request)},
    )


async def _config_page(request: Request, *, saved: str | None = None, msg: str | None = None,
                       error: str | None = None, edit_section: str | None = None,
                       form: dict | None = None, status_code: int = 200):
    """Monta a página de Configurações (usada pelo GET e pelos POSTs que precisam
    reexibir a tela com um erro). `edit_section` ("conta" | "tarifa" | "potencia")
    reabre aquele bloco já em modo de edição, com o que o usuário tinha digitado
    em `form`, pra ele corrigir sem perder o que escreveu."""
    settings = await controller.get_settings()
    if settings is None:  # backend real fora do ar: mostra o último valor em cache
        settings = {
            "contracted_limit_kw": controller.contracted_limit_kw,
            "usable_limit_pct": controller.usable_limit_pct,
            "usable_limit_kw": controller.usable_limit_kw,
        }
    tariff_cfg = await controller.get_tariff_config() or {}
    solar = await controller.get_solar() or {}
    return templates.TemplateResponse(
        "dashboard_configuracoes.html",
        {"request": request, "tariff_cfg": tariff_cfg, "solar": solar, "payment_mode": payments.PAYMENT_MODE,
         "active_nav": "config", "company": _current_company(request), "settings": settings,
         "saved": saved, "msg": msg, "error": error, "edit_section": edit_section, "form": form or {}},
        status_code=status_code,
    )


@app.get("/dashboard/configuracoes", response_class=HTMLResponse)
async def dashboard_configuracoes(request: Request, saved: str | None = None, msg: str | None = None):
    if (redirect := _require_company(request)) is not None:
        return redirect
    return await _config_page(request, saved=saved, msg=msg)


@app.post("/dashboard/configuracoes/conta", response_class=HTMLResponse)
async def dashboard_configuracoes_conta(
    request: Request,
    empresa: str = Form(...),
    responsavel: str = Form(...),
    email: str = Form(...),
):
    """Nome da empresa / responsável / e-mail de acesso. A SENHA não é mais
    alterada por aqui — só pelo link "Alterar senha" (ver rota abaixo), que
    dispara o fluxo de redefinição por e-mail."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    company = _current_company(request)
    form = {"empresa": empresa, "responsavel": responsavel, "email": email}

    email_norm = email.strip().lower()
    error = None
    if not empresa.strip() or not responsavel.strip() or "@" not in email_norm:
        error = "Preencha empresa, responsável e um e-mail válido."
    elif email_norm != company["email"] and company_store.account_exists(email_norm):
        error = "Já existe uma conta com esse e-mail."  # update_company sobrescreveria a outra conta
    if error:
        return await _config_page(request, error=error, edit_section="conta", form=form, status_code=400)

    company_store.update_company(
        company["email"], empresa=empresa, responsavel=responsavel, novo_email=email,
    )
    response = RedirectResponse("/dashboard/configuracoes?saved=1", status_code=303)
    if email_norm != company["email"]:
        # e-mail mudou -> a conta migrou de chave em company_store; reemite
        # o cookie de sessão pro novo e-mail continuar autenticado
        token = company_store.start_session(email)
        response.set_cookie(company_store.SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.post("/dashboard/configuracoes/alterar-senha", response_class=HTMLResponse)
async def dashboard_configuracoes_alterar_senha(request: Request):
    """Link "Alterar senha": em vez de trocar a senha na hora, pede a
    redefinição por e-mail. SIMULADO por enquanto — nenhum e-mail é enviado de
    verdade; só registra no log e mostra o aviso de "e-mail enviado" na tela."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    company = _current_company(request)
    logger.info("[SIMULADO] e-mail de redefinição de senha enviado para %s (empresa)", company["email"])
    return RedirectResponse("/dashboard/configuracoes?msg=senha_email", status_code=303)


@app.post("/dashboard/configuracoes/tarifa", response_class=HTMLResponse)
async def dashboard_configuracoes_tarifa(
    request: Request,
    base: float = Form(...),
    min_mult: float = Form(...),
    max_mult: float = Form(...),
    low_pct: float = Form(...),
    high_pct: float = Form(...),
):
    """Parâmetros da tarifa dinâmica. A regra (validação e cálculo do preço) vive no
    backend real (PUT /api/tariff/config) — aqui só repassa e mostra o erro."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    result = await controller.update_tariff_config(
        base_rs_per_kwh=base, min_multiplier=min_mult, max_multiplier=max_mult,
        low_pressure_pct=low_pct, high_pressure_pct=high_pct,
    )
    if "error" in result:
        return await _config_page(
            request, error=result["error"], edit_section="tarifa",
            form={"base": base, "min_mult": min_mult, "max_mult": max_mult, "low_pct": low_pct, "high_pct": high_pct},
            status_code=400,
        )
    return RedirectResponse("/dashboard/configuracoes?saved=1", status_code=303)


@app.post("/dashboard/configuracoes/solar", response_class=HTMLResponse)
async def dashboard_configuracoes_solar(request: Request, peak_kw: float = Form(...)):
    """Potência FV instalada (kWp). A hora simulada saiu daqui: é do Modo demonstração."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    result = await controller.update_solar(peak_kw=peak_kw)
    if "error" in result:
        return await _config_page(request, error=result["error"], edit_section="solar", status_code=400)
    return RedirectResponse("/dashboard/configuracoes?saved=1", status_code=303)


@app.post("/dashboard/configuracoes/demo", response_class=HTMLResponse)
async def dashboard_configuracoes_demo(request: Request, hora: str = Form("real")):
    """Modo demonstração: congela a hora da simulação solar ("real" = relógio do sistema)."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    try:
        sim_hour = None if hora == "real" else float(hora)
    except ValueError:
        sim_hour = None
    result = await controller.update_solar(sim_hour=sim_hour, set_sim_hour=True)
    if "error" in result:
        return await _config_page(request, error=result["error"], status_code=400)
    return RedirectResponse("/dashboard/configuracoes?saved=1", status_code=303)


@app.post("/dashboard/configuracoes/tarifa-auto", response_class=HTMLResponse)
async def dashboard_configuracoes_tarifa_auto(request: Request):
    """Volta os limiares de pressão da tarifa para a calibração automática pela IA."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    result = await controller.update_tariff_config(pressure_auto=True)
    if "error" in result:
        return await _config_page(request, error=result["error"], edit_section="tarifa", status_code=400)
    return RedirectResponse("/dashboard/configuracoes?saved=1", status_code=303)


@app.post("/dashboard/configuracoes/potencia", response_class=HTMLResponse)
async def dashboard_configuracoes_potencia(
    request: Request,
    contratada_kw: float = Form(...),
    limite_uso_pct: float = Form(...),
):
    """kW contratados + limite de uso antes da redução de potência dos
    carregadores. Toda a regra (validação, redistribuição das sessões ativas)
    vive no backend real (PUT /api/settings) — aqui só repassa e mostra o erro."""
    if (redirect := _require_company(request)) is not None:
        return redirect
    result = await controller.update_settings(contracted_limit_kw=contratada_kw, usable_limit_pct=limite_uso_pct)
    if "error" in result:
        return await _config_page(
            request, error=result["error"], edit_section="potencia",
            form={"contratada_kw": contratada_kw, "limite_uso_pct": limite_uso_pct}, status_code=400,
        )
    return RedirectResponse("/dashboard/configuracoes?saved=1", status_code=303)


# ----------------------------------------------------------------------
# Login de empresa (Dashboard de Gestão) — CPF/senha do cliente é outro
# sistema (client_store.py); aqui é e-mail/senha da empresa/operador.
# ----------------------------------------------------------------------

@app.get("/empresa/login", response_class=HTMLResponse)
async def empresa_login_form(request: Request):
    if _current_company(request):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("empresa/login.html", {"request": request, "error": None, "email": ""})


@app.post("/empresa/login", response_class=HTMLResponse)
async def empresa_login(request: Request, email: str = Form(...), senha: str = Form(...)):
    account = company_store.check_login(email, senha)
    if not account:
        return templates.TemplateResponse(
            "empresa/login.html",
            {"request": request, "error": "E-mail ou senha incorretos.", "email": email},
            status_code=401,
        )
    token = company_store.start_session(account["email"])
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(company_store.SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.post("/empresa/logout")
async def empresa_logout(request: Request):
    company_store.end_session(request.cookies.get(company_store.SESSION_COOKIE))
    response = RedirectResponse("/empresa/login", status_code=303)
    response.delete_cookie(company_store.SESSION_COOKIE)
    return response


# ----------------------------------------------------------------------
# App do Cliente — autenticação (CPF + senha, demo sem segurança real)
# ----------------------------------------------------------------------

def _current_account(request: Request) -> dict | None:
    token = request.cookies.get(client_store.SESSION_COOKIE)
    return client_store.get_account_from_token(token)


def _post_login_destination(cpf: str) -> str:
    """
    P0-4: se a conta já tem uma sessão de carregamento ativa (em
    qualquer veículo), manda direto pro acompanhamento em tempo real em
    vez da home — é assim que o celular do cliente "encontra" a recarga
    que ele iniciou no totem. A navegação normal continua disponível a
    partir de lá (não prende o cliente na tela).
    """
    return "/cliente/carregando" if cpf in client_active_charger else "/cliente/home"


@app.get("/cliente/login", response_class=HTMLResponse)
async def cliente_login_form(request: Request):
    account = _current_account(request)
    if account:
        return RedirectResponse(_post_login_destination(account["cpf"]), status_code=303)
    return templates.TemplateResponse(
        "cliente/login.html",
        {"request": request, "error": None, "cpf": "", "show_forgot": False},
    )


@app.post("/cliente/login", response_class=HTMLResponse)
async def cliente_login(request: Request, cpf: str = Form(...), senha: str = Form(...)):
    if not client_store.account_exists(cpf):
        return templates.TemplateResponse(
            "cliente/login.html",
            {"request": request, "error": "Esse CPF não está cadastrado.", "cpf": cpf, "show_forgot": False},
            status_code=404,
        )
    account = client_store.check_login(cpf, senha)
    if not account:
        return templates.TemplateResponse(
            "cliente/login.html",
            {"request": request, "error": "Senha incorreta.", "cpf": cpf, "show_forgot": True},
            status_code=401,
        )
    token = client_store.start_session(account["cpf"])
    response = RedirectResponse(_post_login_destination(account["cpf"]), status_code=303)
    response.set_cookie(client_store.SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.get("/cliente/cadastro", response_class=HTMLResponse)
async def cliente_cadastro_form(request: Request, cpf: str = ""):
    return templates.TemplateResponse(
        "cliente/cadastro.html",
        {"request": request, "error": None, "form": {"nome": "", "cpf": cpf, "email": ""}},
    )


@app.post("/cliente/cadastro", response_class=HTMLResponse)
async def cliente_cadastro(
    request: Request,
    nome: str = Form(...),
    cpf: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    form = {"nome": nome, "cpf": cpf, "email": email}
    cpf_norm = client_store.normalize_cpf(cpf)

    if len(cpf_norm) != 11:
        return templates.TemplateResponse(
            "cliente/cadastro.html",
            {"request": request, "error": "CPF inválido — deve ter 11 dígitos.", "form": form},
            status_code=400,
        )
    if client_store.account_exists(cpf_norm):
        return templates.TemplateResponse(
            "cliente/cadastro.html",
            {"request": request, "error": "Já existe uma conta com esse CPF. Faça login.", "form": form},
            status_code=409,
        )
    if not senha or senha != confirmar_senha:
        return templates.TemplateResponse(
            "cliente/cadastro.html",
            {"request": request, "error": "As senhas não coincidem (ou estão vazias).", "form": form},
            status_code=400,
        )

    account = client_store.create_account(nome, cpf_norm, email, senha)
    token = client_store.start_session(account["cpf"])
    response = RedirectResponse(_post_login_destination(account["cpf"]), status_code=303)
    response.set_cookie(client_store.SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.get("/cliente/esqueci-senha", response_class=HTMLResponse)
async def cliente_esqueci_senha(request: Request):
    """Só a mensagem/link — sem fluxo de recuperação real (definido com o professor)."""
    return templates.TemplateResponse("cliente/esqueci_senha.html", {"request": request})


@app.post("/cliente/logout")
async def cliente_logout(request: Request):
    client_store.end_session(request.cookies.get(client_store.SESSION_COOKIE))
    response = RedirectResponse("/cliente/login", status_code=303)
    response.delete_cookie(client_store.SESSION_COOKIE)
    return response


# ----------------------------------------------------------------------
# App do Cliente — páginas autenticadas
# ----------------------------------------------------------------------

@app.get("/cliente/home", response_class=HTMLResponse)
async def cliente_home(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    active_charger_id = client_active_charger.get(account["cpf"])
    return templates.TemplateResponse(
        "cliente/home.html",
        {"request": request, "account": account, "active_charger_id": active_charger_id},
    )


def _default_carregar_form() -> dict:
    return {
        "placa": "", "novo_modelo": "", "novo_placa": "", "charger_id": "",
        "modo": "tempo", "minutos": 30, "energia_kwh": 15,
    }


def _carregar_config_response(request: Request, form: dict, error: str | None = None, status_code: int = 200):
    account = _current_account(request)
    payload = build_payload()
    free_chargers = [c for c in payload["chargers"] if c["status"] == "LIVRE"]
    vehicles = client_store.list_vehicles(account["cpf"]) if account else []
    return templates.TemplateResponse(
        "cliente/carregar_config.html",
        {"request": request, "free_chargers": free_chargers, "vehicles": vehicles, "error": error, "form": form,
         "tariff_forecast": controller.tariff_forecast},
        status_code=status_code,
    )


@app.get("/cliente/carregar", response_class=HTMLResponse)
async def cliente_carregar_form(request: Request, pix_erro: str | None = None):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    # P0-6 (ponto de entrada 1 de 2 — o outro é o guard repetido em
    # /resumo e /pagar abaixo, defesa em profundidade): uma sessão ativa
    # por conta, então nem mostra o formulário de nova recarga.
    if account["cpf"] in client_active_charger:
        return RedirectResponse("/cliente/carregando?aviso=ja_ativa", status_code=303)
    if account["cpf"] in pix_pending:
        return RedirectResponse("/cliente/carregar/pix", status_code=303)

    if pix_erro == "timeout":
        error = "Tempo esgotado para concluir o pagamento — tente novamente."
    elif pix_erro:
        error = "O pagamento via Pix não foi aprovado — tente novamente."
    else:
        error = None
    return _carregar_config_response(request, _default_carregar_form(), error=error)


@app.post("/cliente/carregar/resumo", response_class=HTMLResponse)
async def cliente_carregar_resumo(
    request: Request,
    placa: str = Form(...),
    novo_modelo: str = Form(""),
    novo_placa: str = Form(""),
    charger_id: str = Form(...),
    modo: str = Form("tempo"),
    minutos: float | None = Form(None),
    energia_kwh: float | None = Form(None),
):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    if account["cpf"] in client_active_charger:
        return RedirectResponse("/cliente/carregando?aviso=ja_ativa", status_code=303)

    charger_id = charger_id.upper()
    modo = modo if modo in ("tempo", "energia") else "tempo"
    form = {
        "placa": placa, "novo_modelo": novo_modelo, "novo_placa": novo_placa,
        "charger_id": charger_id, "modo": modo, "minutos": minutos, "energia_kwh": energia_kwh,
    }

    # Veículo: ou uma placa já cadastrada foi escolhida, ou o cliente está
    # cadastrando um novo ali mesmo (P0-1) — "__novo__" é o valor sentinela
    # da opção "+ Cadastrar novo veículo" no <select> (ver carregar_config.html).
    existing_plates = {v["placa"] for v in client_store.list_vehicles(account["cpf"])}
    if placa == "__novo__" or placa.strip().upper() not in existing_plates:
        vehicle = client_store.add_vehicle(account["cpf"], novo_modelo, novo_placa)
        if not vehicle:
            return _carregar_config_response(
                request, form,
                error="Selecione um veículo já cadastrado ou informe modelo e placa do novo veículo.",
                status_code=400,
            )
        placa = vehicle["placa"]
    else:
        placa = placa.strip().upper()

    valor = minutos if modo == "tempo" else energia_kwh
    if charger_id not in controller.chargers or not valor or valor <= 0:
        return _carregar_config_response(
            request, form,
            error="Preencha vaga e meta (tempo ou energia) corretamente.",
            status_code=400,
        )

    # LIMITAÇÃO CONHECIDA (documentada, não resolvida nesta rodada): entre o
    # cliente ver a vaga como LIVRE na tela anterior e chegar aqui, outro
    # cliente pode ter escolhido a MESMA vaga — nada reserva a vaga só por
    # ela aparecer no formulário. Esta checagem de is_occupied pega o caso
    # comum, mas ainda existe uma janela de corrida teórica entre dois
    # clientes validando a mesma vaga livre quase ao mesmo tempo (round-trip
    # de rede entre as duas telas). Numa demo single-process o risco é
    # mínimo; resolver de verdade exigiria uma "reserva" da vaga no momento
    # da escolha, não só na confirmação — fica para uma rodada futura.
    if controller.chargers[charger_id].is_occupied:
        form["placa"], form["charger_id"] = placa, ""
        return _carregar_config_response(
            request, form,
            error=f"A vaga {charger_id} acabou de ficar ocupada — escolha outra.",
            status_code=409,
        )

    payload = build_payload()
    charger_info = next(c for c in payload["chargers"] if c["id"] == charger_id)
    estimate = estimate_charge(charger_id, modo, valor)
    return templates.TemplateResponse(
        "cliente/carregar_resumo.html",
        {"request": request, "placa": placa, "charger": charger_info,
         "modo": modo, "valor": valor, "estimate": estimate, "site": SITE_NAME,
         "payment_mode": payments.PAYMENT_MODE},
    )


def _reject_carregar(request: Request, placa: str, message: str, status_code: int = 409):
    form = _default_carregar_form()
    form["placa"] = placa
    return _carregar_config_response(request, form, error=message, status_code=status_code)


async def _start_client_charge(account: dict, charger_id: str, placa: str, goal_fields: dict,
                               event_message: str, tariff: float | None = None) -> str | None:
    """Inicia a recarga de um cliente: conecta o veículo no backend e registra
    a camada do app (meta escolhida, "minha recarga", veículo lembrado na
    conta). Devolve a mensagem de erro (ex.: vaga ocupada) ou None se deu certo.

    Usada pelo pagamento mock (abaixo) e pela demo (/demo/cliente/iniciar).
    O caminho do Pix (/cliente/carregar/pix/status) faz essas mesmas etapas
    depois da confirmação do pagamento — não foi migrado pra cá porque
    exige PAYMENT_MODE=mercadopago pra testar."""
    result = await controller.vehicle_connect(charger_id, placa, tariff)
    if isinstance(result, dict) and "error" in result:
        return result["error"]
    charging_goals[charger_id] = {**goal_fields, "cpf": account["cpf"]}
    client_active_charger[account["cpf"]] = charger_id
    client_store.register_vehicle(account["cpf"], placa)
    controller._log_event("CLIENT_PAYMENT_APPROVED", charger_id, event_message)
    await manager.broadcast(build_payload())
    return None


async def _finish_client_charge(cpf: str, reason: str = "manual") -> dict | None:
    """Encerra a recarga em andamento de um cliente e guarda o recibo (para a
    tela de conclusão). Devolve o resultado do vehicle_disconnect, ou None se
    o cliente não tinha recarga ativa. Usada pelo botão "Encerrar" do app
    (/cliente/carregando/encerrar) e pela demo (/demo/cliente/encerrar)."""
    charger_id = client_active_charger.get(cpf)
    if not charger_id:
        return None
    charger = controller.chargers[charger_id]
    session = charger.session  # referência — vehicle_disconnect() atualiza e arquiva esse mesmo objeto
    location = charger.location

    result = await controller.vehicle_disconnect(charger_id)
    await manager.broadcast(build_payload())

    charging_goals.pop(charger_id, None)
    client_active_charger.pop(cpf, None)

    if session is not None and not (isinstance(result, dict) and "error" in result):
        # Dados já existem em completed_sessions — só formata pra tela de conclusão.
        last_receipt[cpf] = _build_receipt(session, charger_id, location, reason)
    return result


@app.post("/cliente/carregar/pagar", response_class=HTMLResponse)
async def cliente_carregar_pagar(
    request: Request,
    placa: str = Form(...),
    charger_id: str = Form(...),
    modo: str = Form(...),
    valor: float = Form(...),
    metodo_pagamento: str = Form(...),
    tarifa: float | None = Form(None),
):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    if account["cpf"] in client_active_charger:
        return RedirectResponse("/cliente/carregando?aviso=ja_ativa", status_code=303)

    charger_id = charger_id.upper()
    placa = placa.strip().upper()
    modo = modo if modo in ("tempo", "energia") else "tempo"
    goal_fields = {
        "goal_type": modo,
        "target_min": valor if modo == "tempo" else None,
        "target_kwh": valor if modo == "energia" else None,
    }
    # Preço que o motorista VIU no resumo — fica travado na sessão (o backend limita à faixa válida).
    locked_tariff = tarifa if tarifa and tarifa > 0 else current_price()

    if payments.PAYMENT_MODE == "mock":
        # Modo de contingência (ver .env): comportamento antigo,
        # inalterado — todo pagamento é aprovado instantaneamente, sem
        # chamar a API externa. Existe para a demo sobreviver a uma falha
        # de rede na sala, trocando só PAYMENT_MODE=mock e reiniciando.
        error = await _start_client_charge(
            account, charger_id, placa, goal_fields,
            f"[MOCK] Pagamento ({metodo_pagamento}) aprovado — recarga iniciada pelo cliente {account['nome']}",
            tariff=locked_tariff,
        )
        if error:
            return _reject_carregar(request, placa, f"Não foi possível iniciar a recarga: {error}")
        # P0-3: o totem não acompanha mais a recarga — só confirma e some
        # sozinho (ver /cliente/carregar/iniciada). O acompanhamento em
        # tempo real (/cliente/carregando) passa a ser alcançado por login.
        return RedirectResponse("/cliente/carregar/iniciada", status_code=303)

    # PAYMENT_MODE == "mercadopago": gera uma cobrança Pix real (sandbox) e
    # manda o cliente pra tela de QR. vehicle_connect() só acontece quando
    # o status vier aprovado — ver GET /cliente/carregar/pix/status (polling).
    #
    # LIMITAÇÃO CONHECIDA (mesma da /resumo, ver comentário lá): a vaga só é
    # revalidada aqui, no momento de gerar o Pix — ainda existe uma janela
    # teórica entre isso e o cliente ter chegado nesta tela em que outro
    # cliente poderia ter escolhido a mesma vaga. Não resolvido nesta rodada.
    if charger_id not in controller.chargers or controller.chargers[charger_id].is_occupied:
        return _reject_carregar(request, placa, f"A vaga {charger_id} não está mais disponível — escolha outra.")

    estimate = estimate_charge(charger_id, modo, valor, locked_tariff)
    if estimate["estimated_cost_rs"] is None:
        # Rede sem folga alguma agora (0 kW disponíveis) — não dá pra cobrar
        # um valor que nem foi possível estimar. Evita crashar create_pix_charge()
        # tentando formatar None como número.
        return _reject_carregar(request, placa, "Sem potência disponível na rede agora para estimar o valor — tente novamente em instantes.")

    external_reference = f"chargegrid-{charger_id}-{account['cpf']}-{int(datetime.now().timestamp())}"

    try:
        charge = payments.create_pix_charge(
            valor_rs=estimate["estimated_cost_rs"],
            descricao=f"ChargeGrid - recarga ({modo}) em {charger_id}",
            external_reference=external_reference,
        )
    except Exception as exc:
        payload = build_payload()
        charger_info = next(c for c in payload["chargers"] if c["id"] == charger_id)
        return templates.TemplateResponse(
            "cliente/carregar_resumo.html",
            {"request": request, "placa": placa, "charger": charger_info,
             "modo": modo, "valor": valor, "estimate": estimate, "site": SITE_NAME,
             "payment_mode": payments.PAYMENT_MODE,
             "error": f"Não foi possível gerar o Pix agora: {exc}"},
            status_code=502,
        )

    pix_pending[account["cpf"]] = {
        "charger_id": charger_id,
        "placa": placa,
        **goal_fields,
        "order_id": charge["order_id"],
        "qr_code": charge["qr_code"],
        "qr_code_base64": charge["qr_code_base64"],
        "valor_rs": estimate["estimated_cost_rs"],
        "tarifa": locked_tariff,
        "created_at": datetime.now(),  # P0-8: base do timeout de ~5 min do totem
    }
    controller._log_event(
        "CLIENT_PIX_CREATED", charger_id,
        f"[MERCADOPAGO] Pix de {format_brl(estimate['estimated_cost_rs'])} gerado para {account['nome']} — aguardando pagamento",
    )
    return RedirectResponse("/cliente/carregar/pix", status_code=303)


def _pix_expired(pending: dict) -> bool:
    return (datetime.now() - pending["created_at"]).total_seconds() > PIX_TIMEOUT_S


@app.get("/cliente/carregar/pix", response_class=HTMLResponse)
async def cliente_carregar_pix(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    pending = pix_pending.get(account["cpf"])
    if not pending:
        return RedirectResponse("/cliente/home", status_code=303)
    if _pix_expired(pending):
        # P0-8: cliente recarregou a tela depois do prazo sem o polling ter
        # rodado — mesmo tratamento do timeout detectado em /pix/status.
        pix_pending.pop(account["cpf"], None)
        controller._log_event(
            "CLIENT_PIX_TIMEOUT", pending["charger_id"],
            f"[MERCADOPAGO] Pix expirou sem confirmação (~{PIX_TIMEOUT_S // 60} min) para {account['nome']}",
        )
        return RedirectResponse("/cliente/carregar?pix_erro=timeout", status_code=303)
    return templates.TemplateResponse(
        "cliente/carregar_pix.html",
        {"request": request, "pending": pending, "site": SITE_NAME},
    )


@app.get("/cliente/carregar/pix/status", response_class=HTMLResponse)
async def cliente_carregar_pix_status(request: Request):
    """
    Alvo do polling HTMX (a cada ~3s) da tela de QR — NÃO é webhook,
    é o próprio ChargeGrid perguntando pro Mercado Pago "já pagou?"
    (ver payments.get_charge_status). Também é o alvo do botão manual
    "Já paguei": clicar nele só antecipa esta mesma checagem, não marca
    nada como pago por conta própria.
    """
    account = _current_account(request)
    if not account:
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/cliente/login"
        return response

    pending = pix_pending.get(account["cpf"])
    if not pending:
        # Resolvido em outra aba, ou expirou — manda pra home.
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/cliente/home"
        return response

    if _pix_expired(pending):
        # P0-8: totem (ou qualquer dispositivo) volta sozinho pro início se
        # o Pix não confirmar em ~5 min — não cancela nada do lado do
        # Mercado Pago, só desiste de esperar por aqui.
        pix_pending.pop(account["cpf"], None)
        controller._log_event(
            "CLIENT_PIX_TIMEOUT", pending["charger_id"],
            f"[MERCADOPAGO] Pix expirou sem confirmação (~{PIX_TIMEOUT_S // 60} min) para {account['nome']}",
        )
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/cliente/carregar?pix_erro=timeout"
        return response

    try:
        status_info = payments.get_charge_status(pending["order_id"])
    except Exception as exc:
        return templates.TemplateResponse(
            "partials/pix_status.html",
            {"request": request, "state": "error", "message": str(exc)},
        )

    if status_info["status"] == "pending":
        # Normalmente o QR já veio pronto na criação (ver payments.create_pix_charge).
        # Mas se não veio, este polling que já roda a cada ~3s é quem completa
        # sozinho assim que a consulta trouxer o dado — sem exigir reload da página.
        qr_just_arrived = False
        if not pending.get("qr_code") and status_info.get("qr_code"):
            pending["qr_code"] = status_info["qr_code"]
            pending["qr_code_base64"] = status_info["qr_code_base64"]
            qr_just_arrived = True
        return templates.TemplateResponse(
            "partials/pix_status.html",
            {"request": request, "state": "pending", "pending": pending, "qr_just_arrived": qr_just_arrived},
        )

    charger_id = pending["charger_id"]

    if status_info["status"] == "rejected":
        pix_pending.pop(account["cpf"], None)
        controller._log_event(
            "CLIENT_PIX_REJECTED", charger_id,
            f"[MERCADOPAGO] Pix não aprovado para {account['nome']} "
            f"({status_info['raw_status']}/{status_info['raw_status_detail']})",
        )
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/cliente/carregar?pix_erro=1"
        return response

    # status_info["status"] == "approved" — só agora conecta de verdade.
    result = await controller.vehicle_connect(charger_id, pending["placa"], pending.get("tarifa"))
    pix_pending.pop(account["cpf"], None)

    if isinstance(result, dict) and "error" in result:
        # Pagamento aprovado do lado do Mercado Pago, mas a vaga ficou
        # ocupada por outra sessão nesse meio-tempo. Fora do escopo desta
        # demo estornar automaticamente — loga bem alto pro operador ver.
        controller._log_event(
            "CLIENT_PIX_APPROVED_BUT_CHARGER_TAKEN", charger_id,
            f"[MERCADOPAGO] Pix aprovado para {account['nome']} mas {charger_id} "
            f"ficou ocupado antes — verificar manualmente.",
        )
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/cliente/home"
        return response

    charging_goals[charger_id] = {
        "goal_type": pending["goal_type"], "target_min": pending["target_min"],
        "target_kwh": pending["target_kwh"], "cpf": account["cpf"],
    }
    client_active_charger[account["cpf"]] = charger_id
    client_store.register_vehicle(account["cpf"], pending["placa"])
    controller._log_event(
        "CLIENT_PAYMENT_APPROVED", charger_id,
        f"[MERCADOPAGO] Pix aprovado — recarga iniciada pelo cliente {account['nome']}",
    )
    await manager.broadcast(build_payload())

    # P0-3: mesmo destino do modo mock — confirmação curta, não mais a tela
    # de acompanhamento (ver /cliente/carregar/iniciada).
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = "/cliente/carregar/iniciada"
    return response


@app.get("/cliente/carregar/iniciada", response_class=HTMLResponse)
async def cliente_carregar_iniciada(request: Request):
    """
    P0-3: tela de confirmação curta do totem — substitui a antiga tela de
    'carregamento em andamento' que ficava presa na conta do cliente num
    dispositivo compartilhado. Faz logout automático (ver app.js
    `scheduleAutoLogout`) e volta pro login sozinha; o acompanhamento de
    verdade agora vive em /cliente/carregando, alcançado por login (P0-4).
    """
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    charger_id = client_active_charger.get(account["cpf"])
    if not charger_id:
        return RedirectResponse("/cliente/home", status_code=303)
    charger = controller.chargers.get(charger_id)
    placa = charger.session.vehicle_id if charger and charger.session else "—"
    location = charger.location if charger else charger_id

    # P1-2: QR code que abre o acompanhamento direto no celular, sem
    # precisar digitar CPF/senha de novo — ver /cliente/sessao/{token}.
    qr_url = _issue_session_qr_url(request, account["cpf"])
    qr_base64 = generate_qr_base64(qr_url)

    return templates.TemplateResponse(
        "cliente/carregar_iniciada.html",
        {"request": request, "charger_id": charger_id, "location": location, "placa": placa, "qr_base64": qr_base64},
    )


@app.get("/cliente/sessao/{token}")
async def cliente_sessao_token(token: str):
    """
    P1-2: atalho do QR code da tela de confirmação do totem — NÃO
    substitui o login manual (que continua funcionando normalmente),
    é só uma conveniência a mais. Token de uso único: é removido do
    dict aqui, na leitura, mesmo se já estiver expirado — não tem como
    reutilizar a mesma URL uma segunda vez.
    """
    entry = session_qr_tokens.pop(token, None)
    if not entry or entry["expires_at"] < datetime.now():
        return RedirectResponse("/cliente/login", status_code=303)

    cpf = entry["cpf"]
    if not client_store.account_exists(cpf):
        return RedirectResponse("/cliente/login", status_code=303)

    session_token = client_store.start_session(cpf)
    response = RedirectResponse(_post_login_destination(cpf), status_code=303)
    response.set_cookie(client_store.SESSION_COOKIE, session_token, httponly=True, samesite="lax")
    return response


@app.get("/cliente/carregando", response_class=HTMLResponse)
async def cliente_carregando(request: Request, aviso: str | None = None):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)

    charger_id = client_active_charger.get(account["cpf"])
    if not charger_id:
        return RedirectResponse("/cliente/home", status_code=303)

    payload = build_payload()
    charger_state = next((c for c in payload["chargers"] if c["id"] == charger_id), None)

    # Se um operador forçou a parada pelo Dashboard enquanto o cliente estava
    # nesta tela, a vaga já não está mais OCUPADA — encerra a associação e
    # manda o cliente de volta pro início em vez de mostrar uma tela travada.
    if not charger_state or charger_state["status"] != "OCUPADO":
        client_active_charger.pop(account["cpf"], None)
        charging_goals.pop(charger_id, None)
        return RedirectResponse("/cliente/home", status_code=303)

    goal = charging_goals.get(charger_id, {"goal_type": "energia", "target_kwh": 0.0, "target_min": None})
    aviso_msg = "Você já tem uma recarga em andamento." if aviso == "ja_ativa" else None
    return templates.TemplateResponse(
        "cliente/carregando.html",
        {"request": request, "charger": charger_state,
         "tariff": charger_state.get("tariff_rs_per_kwh") or current_price(),
         "site": SITE_NAME, "goal": goal, "aviso": aviso_msg},
    )


@app.post("/cliente/carregando/encerrar", response_class=HTMLResponse)
async def cliente_carregando_encerrar(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)

    charger_id = client_active_charger.get(account["cpf"])
    if not charger_id:
        # P0-5 (idempotência): a meta pode ter sido atingida e encerrada
        # sozinha pelo ticker (_check_goals_and_autodisconnect) bem antes
        # deste clique chegar — o recibo já existe, então manda pra ele em
        # vez de tratar como erro / clique perdido.
        if account["cpf"] in last_receipt:
            return RedirectResponse("/cliente/carregando/concluido", status_code=303)
        return RedirectResponse("/cliente/home", status_code=303)

    await _finish_client_charge(account["cpf"], "manual")
    return RedirectResponse("/cliente/carregando/concluido", status_code=303)


@app.get("/cliente/carregando/concluido", response_class=HTMLResponse)
async def cliente_carregando_concluido(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    receipt = last_receipt.get(account["cpf"])
    if not receipt:
        return RedirectResponse("/cliente/home", status_code=303)
    return templates.TemplateResponse("cliente/conclusao.html", {"request": request, "r": receipt, "site": SITE_NAME})


@app.get("/cliente/perfil", response_class=HTMLResponse)
async def cliente_perfil(request: Request, saved: str | None = None, msg: str | None = None):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    return templates.TemplateResponse(
        "cliente/perfil.html",
        {"request": request, "account": account, "saved": saved, "msg": msg, "error": None, "form": {}},
    )


@app.post("/cliente/perfil/atualizar", response_class=HTMLResponse)
async def cliente_perfil_atualizar(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
):
    """Nome e e-mail da conta do cliente. A SENHA não é mais alterada por aqui —
    só pelo link "Alterar senha" (rota abaixo), que dispara a redefinição por e-mail."""
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    if not nome.strip() or "@" not in email:
        return templates.TemplateResponse(
            "cliente/perfil.html",
            {"request": request, "account": account, "saved": None, "msg": None,
             "error": "Preencha o nome e um e-mail válido.", "form": {"nome": nome, "email": email}},
            status_code=400,
        )
    client_store.update_account(account["cpf"], nome=nome, email=email)
    return RedirectResponse("/cliente/perfil?saved=1", status_code=303)


@app.post("/cliente/perfil/alterar-senha", response_class=HTMLResponse)
async def cliente_perfil_alterar_senha(request: Request):
    """Link "Alterar senha": pede a redefinição por e-mail em vez de trocar a
    senha na hora. SIMULADO por enquanto — nenhum e-mail é enviado de verdade
    (mesma abordagem do /cliente/esqueci-senha); só mostra o aviso na tela."""
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    logger.info("[SIMULADO] e-mail de redefinição de senha enviado para %s (cliente)", account["email"])
    return RedirectResponse("/cliente/perfil?msg=senha_email", status_code=303)


@app.post("/cliente/perfil/excluir", response_class=HTMLResponse)
async def cliente_perfil_excluir(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    client_store.delete_account(account["cpf"])
    response = RedirectResponse("/cliente/login", status_code=303)
    response.delete_cookie(client_store.SESSION_COOKIE)
    return response


@app.get("/cliente/veiculos", response_class=HTMLResponse)
async def cliente_veiculos(request: Request, erro: str | None = None):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    vehicles = client_store.list_vehicles(account["cpf"])
    return templates.TemplateResponse(
        "cliente/veiculos.html", {"request": request, "account": account, "vehicles": vehicles, "erro": erro}
    )


@app.post("/cliente/veiculos/adicionar", response_class=HTMLResponse)
async def cliente_veiculos_adicionar(request: Request, modelo: str = Form(""), placa: str = Form(...)):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    if not placa.strip():
        return RedirectResponse("/cliente/veiculos?erro=placa_vazia", status_code=303)
    client_store.add_vehicle(account["cpf"], modelo, placa)
    return RedirectResponse("/cliente/veiculos", status_code=303)


@app.post("/cliente/veiculos/remover", response_class=HTMLResponse)
async def cliente_veiculos_remover(request: Request, placa: str = Form(...)):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    client_store.remove_vehicle(account["cpf"], placa)
    return RedirectResponse("/cliente/veiculos", status_code=303)


@app.get("/cliente/historico", response_class=HTMLResponse)
async def cliente_historico(request: Request):
    account = _current_account(request)
    if not account:
        return RedirectResponse("/cliente/login", status_code=303)
    plates = set(account.get("veiculos", []))
    sessions = [s for s in completed_sessions_payload() if s["vehicle_id"] in plates]
    sessions.reverse()
    return templates.TemplateResponse("cliente/historico.html", {"request": request, "account": account, "sessions": sessions})


# ----------------------------------------------------------------------
# App do Cliente — estimativa de recarga (NÃO toca no DemandController)
# ----------------------------------------------------------------------

@app.post("/estimate")
async def estimate(charger_id: str = Form(...), modo: str = Form("energia"), valor: float = Form(...)):
    charger_id = charger_id.upper()
    modo = modo if modo in ("tempo", "energia") else "energia"
    if charger_id not in controller.chargers:
        raise HTTPException(status_code=404, detail="Carregador não encontrado.")
    if valor <= 0:
        raise HTTPException(status_code=400, detail="Valor da meta deve ser maior que zero.")
    return JSONResponse(estimate_charge(charger_id, modo, valor))


# ----------------------------------------------------------------------
# Rotas — HTMX partials
# ----------------------------------------------------------------------

def _require_charger(charger_id: str) -> str:
    charger_id = charger_id.upper()
    if charger_id not in controller.chargers:
        raise HTTPException(status_code=404, detail="Carregador não encontrado.")
    return charger_id


def charger_details_response(request: Request, charger_id: str) -> HTMLResponse:
    payload = build_payload()
    charger_state = next(c for c in payload["chargers"] if c["id"] == charger_id)
    return templates.TemplateResponse(
        "partials/charger_details.html",
        {"request": request, "c": charger_state},
    )


@app.get("/charger/{charger_id}/details", response_class=HTMLResponse)
async def charger_details(request: Request, charger_id: str):
    charger_id = _require_charger(charger_id)
    return charger_details_response(request, charger_id)


@app.get("/charger/{charger_id}/events", response_class=HTMLResponse)
async def charger_events(request: Request, charger_id: str):
    """Log de eventos completo (não só os últimos 5 globais), filtrado por carregador."""
    charger_id = _require_charger(charger_id)
    events = [e for e in controller.events if e["source"] == charger_id][-20:]
    events.reverse()
    return templates.TemplateResponse(
        "partials/charger_events.html",
        {"request": request, "events": events, "charger_id": charger_id},
    )


@app.post("/charger/{charger_id}/demo/payment", response_class=HTMLResponse)
async def set_payment_status(request: Request, charger_id: str, status: str = Form(...)):
    """[DEMO] Marca o status de pagamento mockado — não é uma integração real (ver P2-13)."""
    _require_operator(request)
    charger_id = _require_charger(charger_id)
    if status not in ("pago", "pendente"):
        raise HTTPException(status_code=400, detail="Status de pagamento inválido.")
    payment_status[charger_id] = status
    controller._log_event("DEMO_PAYMENT_FLAG", charger_id, f"[DEMO] pagamento marcado como '{status}'")
    await manager.broadcast(build_payload())
    return charger_details_response(request, charger_id)


@app.post("/charger/{charger_id}/demo/flag", response_class=HTMLResponse)
async def set_sim_flag(request: Request, charger_id: str, flag: str = Form(...)):
    """[DEMO] Liga/desliga a flag visual de manutenção/erro — camada separada do
    DemandController, nunca entra no algoritmo de redistribuição (ver P2-12)."""
    _require_operator(request)
    charger_id = _require_charger(charger_id)
    if flag not in ("none", "manutencao", "erro"):
        raise HTTPException(status_code=400, detail="Flag de simulação inválida.")
    sim_flags[charger_id] = None if flag == "none" else flag
    label = "removida" if flag == "none" else f"marcada como '{flag}'"
    controller._log_event("DEMO_SIM_FLAG", charger_id, f"[DEMO] flag de simulação {label}")
    await manager.broadcast(build_payload())
    return charger_details_response(request, charger_id)


@app.post("/chat", response_class=HTMLResponse)
async def chat(request: Request, message: str = Form(...)):
    _require_operator(request)
    reply = chatbot.answer(message, controller)
    return templates.TemplateResponse(
        "partials/chat_message.html",
        {"request": request, "user_message": message, "reply": reply},
    )


# ----------------------------------------------------------------------
# Rotas — ações reais no DemandController
# ----------------------------------------------------------------------

def _free_registered_plate() -> str | None:
    """Primeiro veículo CADASTRADO em alguma conta que não esteja carregando agora.
    Substitui o antigo sorteio de placa aleatória do /connect: quando ninguém
    diz qual veículo conectar, usa o de um cliente de verdade da base."""
    in_use = {s.vehicle_id for s in controller.active_sessions}
    for account in client_store.accounts.values():
        for v in account.get("veiculos_cadastrados", []):
            if v["placa"] not in in_use:
                return v["placa"]
    return None


@app.post("/connect")
async def connect(request: Request, charger_id: str = Form(...), vehicle_id: str | None = Form(None)):
    _require_operator(request)
    charger_id = charger_id.upper()
    if charger_id not in controller.chargers:
        raise HTTPException(status_code=404, detail="Carregador não encontrado.")
    plate = (vehicle_id or "").strip() or _free_registered_plate()
    if not plate:
        raise HTTPException(status_code=400, detail="Informe vehicle_id — não há veículo cadastrado livre para usar no lugar.")
    result = await controller.vehicle_connect(charger_id, plate)
    await manager.broadcast(build_payload())
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse({"ok": True})


@app.post("/disconnect")
async def disconnect(request: Request, charger_id: str = Form(...)):
    _require_operator(request)
    charger_id = charger_id.upper()
    if charger_id not in controller.chargers:
        raise HTTPException(status_code=404, detail="Carregador não encontrado.")
    result = await controller.vehicle_disconnect(charger_id)
    await manager.broadcast(build_payload())
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------
# Rotas de demonstração (DEMO_MODE) — usadas pelos scripts de bastidor
# (scripts/demo_scenario.py e scripts/seed_demo.py). Iniciam/encerram a
# recarga de um CLIENTE FICTÍCIO pela mesma camada do app do cliente
# (_start_client_charge/_finish_client_charge): meta escolhida, "minha
# recarga", recibo e histórico ficam amarrados à conta — só o pagamento é
# pulado. Exigem login de operador OU o cabeçalho X-API-Key (CHARGEGRID_API_KEY); sem chave
# configurada (dev local), aceitam chamadas da própria máquina — os scripts não têm cookie.
# ----------------------------------------------------------------------

def _require_demo(request: Request) -> None:
    if not DEMO_MODE:
        raise HTTPException(status_code=404, detail="Modo demonstração desligado (DEMO_MODE=0).")
    _require_operator_or_key(request)


def _demo_account(cpf: str) -> dict:
    account = client_store.accounts.get(client_store.normalize_cpf(cpf))
    if not account or not account.get("demo"):
        raise HTTPException(status_code=404, detail="Cliente de demonstração não encontrado.")
    return account


@app.get("/demo/clientes")
async def demo_clientes(request: Request):
    """Os clientes fictícios (com veículos e se estão carregando agora) — é daqui que os scripts de demo escolhem quem usar."""
    _require_demo(request)
    return JSONResponse([
        {
            "cpf": a["cpf"], "nome": a["nome"], "email": a["email"], "senha": demo_seed.DEMO_PASSWORD,
            "veiculos": a.get("veiculos_cadastrados", []),
            "carregando_em": client_active_charger.get(a["cpf"]),
        }
        for a in client_store.accounts.values() if a.get("demo")
    ])


@app.post("/demo/cliente/iniciar")
async def demo_cliente_iniciar(
    request: Request,
    cpf: str = Form(...),
    charger_id: str = Form(...),
    placa: str = Form(""),
    modo: str = Form("tempo"),
    valor: float = Form(30.0),
):
    _require_demo(request)
    account = _demo_account(cpf)
    charger_id = charger_id.upper()
    if charger_id not in controller.chargers:
        raise HTTPException(status_code=404, detail="Carregador não encontrado.")
    if account["cpf"] in client_active_charger:
        raise HTTPException(status_code=409, detail=f"{account['nome']} já está carregando na vaga {client_active_charger[account['cpf']]}.")

    own_plates = [v["placa"] for v in account.get("veiculos_cadastrados", [])]
    placa = (placa or "").strip().upper() or (own_plates[0] if own_plates else "")
    if placa not in own_plates:
        raise HTTPException(status_code=400, detail=f"A placa {placa or '(vazia)'} não é um veículo cadastrado de {account['nome']}.")
    modo = modo if modo in ("tempo", "energia") else "tempo"
    goal_fields = {
        "goal_type": modo,
        "target_min": valor if modo == "tempo" else None,
        "target_kwh": valor if modo == "energia" else None,
    }
    error = await _start_client_charge(
        account, charger_id, placa, goal_fields,
        f"[DEMO] Recarga iniciada pelo cliente {account['nome']} (pagamento simulado)",
    )
    if error:
        raise HTTPException(status_code=409, detail=error)
    return JSONResponse({"ok": True, "cliente": account["nome"], "placa": placa, "charger_id": charger_id})


@app.post("/demo/cliente/encerrar")
async def demo_cliente_encerrar(request: Request, cpf: str = Form(...)):
    _require_demo(request)
    account = _demo_account(cpf)
    result = await _finish_client_charge(account["cpf"], "manual")
    if result is None:
        raise HTTPException(status_code=409, detail=f"{account['nome']} não tem recarga em andamento.")
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse({"ok": True, "cliente": account["nome"]})


@app.get("/sessions")
async def sessions(request: Request):
    _require_operator(request)
    return JSONResponse(completed_sessions_payload())


# ----------------------------------------------------------------------
# WebSocket — broadcast de estado em tempo real
# ----------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # O estado em tempo real só vai para quem está logado (operador OU cliente).
    if (company_store.get_company_from_token(websocket.cookies.get(company_store.SESSION_COOKIE)) is None
            and client_store.get_account_from_token(websocket.cookies.get(client_store.SESSION_COOKIE)) is None):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        await websocket.send_json(build_payload())
        while True:
            # Mantém a conexão viva; o cliente não precisa enviar nada.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
