"""
ChargeGrid Intelligence — Assistente IA (baseado em regras)
-------------------------------------------------------------
Responde perguntas do operador sobre o estado atual do sistema.
Não depende de nenhum LLM externo — é um motor de regras/palavras-chave
que lê diretamente o estado do DemandController. Pensado para ser
substituído por um modelo real numa fase futura sem mudar o contrato
(`answer(message, controller) -> str`).
"""

import re
import unicodedata

from remote_controller import RemoteController


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def _norm(text: str) -> str:
    return _strip_accents(text).lower().strip()


def _brl(value: float) -> str:
    """Formata um valor como moeda no padrão brasileiro: 'R$ 1.234,56'."""
    s = f"{value:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {s}"


def _billing_today(controller: RemoteController) -> dict:
    sessions = list(controller.active_sessions) + list(controller.today_completed_sessions)
    total_kwh = sum(s.energy_consumed_kwh for s in sessions)
    return {
        "total_kwh": total_kwh,
        "revenue_rs": sum(s.cost_rs for s in sessions),
        "sessions_today": len(sessions),
    }


def _find_charger_id(message: str, controller: RemoteController) -> str | None:
    match = re.search(r"cg[\s-]?0?(\d{1,2})", message, re.IGNORECASE)
    if not match:
        return None
    candidate = f"CG-{int(match.group(1)):02d}"
    return candidate if candidate in controller.chargers else None


def answer(message: str, controller: RemoteController) -> str:
    """Retorna uma resposta em texto para a mensagem do operador."""
    text = _norm(message)

    charger_id = _find_charger_id(text, controller)
    if charger_id:
        c = controller.chargers[charger_id]
        if c.is_occupied:
            s = c.session
            return (
                f"{charger_id} ({c.location}) está OCUPADO — veículo {s.vehicle_id}, "
                f"{c.current_kw:.2f} kW alocados, {s.duration_minutes():.1f} min de sessão, "
                f"{s.energy_consumed_kwh:.3f} kWh consumidos."
            )
        return f"{charger_id} ({c.location}) está LIVRE, pronto para receber um veículo."

    if any(k in text for k in ["energia por carregador", "energia de cada", "kwh por carregador"]):
        parts = []
        for cid in sorted(controller.chargers):
            c = controller.chargers[cid]
            kwh = c.session.energy_consumed_kwh if c.session else 0.0
            parts.append(f"{cid}: {kwh:.3f} kWh")
        return "Energia consumida por carregador — " + "; ".join(parts) + "."

    if any(k in text for k in ["livre", "disponivel", "vaga livre", "quantos livres"]):
        free = [c.charger_id for c in controller.chargers.values() if not c.is_occupied]
        if not free:
            return "Nenhum carregador livre no momento — os 12 pontos estão ocupados."
        return f"{len(free)} carregador(es) livre(s): {', '.join(free)}."

    if any(k in text for k in ["ocupad", "quantos carro", "sessoes ativas", "sessao ativa", "em uso"]):
        n = len(controller.active_sessions)
        return f"{n} carregador(es) em uso agora, de um total de {len(controller.chargers)}."

    if any(k in text for k in ["fatur", "receita", "quanto vend", "quanto ganh", "dinheiro"]):
        b = _billing_today(controller)
        return (
            f"Faturamento acumulado hoje: {_brl(b['revenue_rs'])} "
            f"({b['total_kwh']:.3f} kWh, cada sessão com a tarifa dinâmica travada na conexão), "
            f"em {b['sessions_today']} sessão(ões)."
        )

    if any(k in text for k in ["solar", "fotovolt", "sol "]) or text.endswith("sol"):
        snap = controller._state_snapshot()
        return (
            f"Geração solar agora: {snap.get('solar_kw', 0):.1f} kW (de {snap.get('solar_peak_kw', 0):g} kWp instalados). "
            f"Orçamento dos carregadores: {snap.get('budget_kw', 0):.1f} kW "
            f"({controller.usable_limit_kw:.1f} da rede + solar)."
        )

    if any(k in text for k in ["verde", "co2", "carbono", "sustent", "renovavel"]):
        snap = controller._state_snapshot()
        return (
            f"{snap.get('green_share_pct', 0):.1f}% da energia entregue veio do solar "
            f"({snap.get('green_kwh_total', 0):.2f} kWh verdes), "
            f"evitando ~{snap.get('co2_avoided_kg', 0):.3f} kg de CO₂ (fator médio da rede brasileira, bem baixo)."
        )

    if any(k in text for k in ["tarifa", "preco", "valor do kwh", "quanto custa"]):
        t = controller.tariff
        if not t:
            return "Ainda não consegui ler a tarifa do backend."
        return (
            f"Preço do kWh agora: {_brl(t['price_rs_per_kwh'])} (tarifa {t['level']}; "
            f"{t['multiplier']:.2f}× a base de {_brl(t['base_rs_per_kwh'])}). "
            f"A IA prevê a pressão sobre a rede e encarece os horários de pico — pressão atual: {t['pressure_pct']:.0f}%."
        )

    if any(k in text for k in ["limite", "utilizacao", "capacidade", "quanto esta usado"]):
        return (
            f"Utilização atual: {controller.utilization_pct:.1f}% "
            f"({controller.total_allocated_kw:.1f} kW de {controller.usable_limit_kw:.1f} kW utilizáveis, "
            f"{controller.available_kw:.1f} kW ainda disponíveis)."
        )

    if any(k in text for k in ["ola", "oi", "bom dia", "boa tarde", "boa noite"]):
        return "Olá! Posso informar vagas livres, faturamento do dia, utilização do barramento ou o status de um carregador específico (ex.: \"status do CG-07\")."

    return (
        "Não entendi completamente. Posso responder sobre: carregadores livres/ocupados, "
        "faturamento do dia, utilização do limite contratado, tarifa aplicada, ou o status "
        "de um carregador específico (ex.: \"CG-03\")."
    )
