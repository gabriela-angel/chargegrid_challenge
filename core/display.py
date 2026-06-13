"""
ChargeGrid Intelligence — Terminal Display
------------------------------------------
Camada de apresentação para o terminal.
Totalmente separada da lógica de negócio.

Quando migrar para web sera substituido por uma API REST
(FastAPI/Flask) que serve os mesmos dados do state_snapshot().
"""

import os
import time


# ANSI colors
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    DIM    = "\033[2m"
    BG_DARK = "\033[40m"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def bar(value_pct: float, width: int = 30) -> str:
    """Gera uma barra de progresso colorida no terminal."""
    filled = int(width * value_pct / 100)
    empty = width - filled

    if value_pct < 60:
        color = C.GREEN
    elif value_pct < 85:
        color = C.YELLOW
    else:
        color = C.RED

    return f"{color}{'█' * filled}{C.DIM}{'░' * empty}{C.RESET}"


def render_dashboard(state: dict):
    """
    Renderiza o estado completo do sistema no terminal.
    Recebe apenas o dicionário de estado — não conhece a lógica de negócio.
    """
    clear()

    util = state["utilization_pct"]
    total = state["total_allocated_kw"]
    limit = state["contracted_limit_kw"]
    usable = state["usable_limit_kw"]
    available = state["available_kw"]
    active = state["active_sessions"]

    # ── Header ───────────────────────────────────────────────────────
    print(f"\n{C.BOLD}{C.CYAN}{'═' * 66}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  ⚡ ChargeGrid Intelligence — {state['site']}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'═' * 66}{C.RESET}")
    print(f"  {C.DIM}{time.strftime('%d/%m/%Y %H:%M:%S')}{C.RESET}\n")

    # ── Painel de potência ────────────────────────────────────────────
    print(f"{C.BOLD}  DEMANDA DA REDE{C.RESET}")
    print(f"  {bar(util)}  {C.BOLD}{util:.1f}%{C.RESET}")
    print()
    print(f"  Alocado:     {C.BOLD}{C.YELLOW}{total:.1f} kW{C.RESET}")
    print(f"  Disponível:  {C.GREEN}{available:.1f} kW{C.RESET}")
    print(f"  Limite útil: {usable:.1f} kW  {C.DIM}(contratado: {limit:.0f} kW){C.RESET}")
    print(f"  Sessões ativas: {C.BOLD}{active}{C.RESET}\n")

    # Alerta de demanda alta
    if util >= 85:
        print(f"  {C.RED}{C.BOLD}⚠  ATENÇÃO: Demanda em {util:.0f}% do limite!{C.RESET}\n")

    # ── Carregadores ─────────────────────────────────────────────────
    print(f"{C.BOLD}  CARREGADORES{C.RESET}")
    print(f"  {'─' * 87}")
    print(f"  {'ID':<8} {'LOCAL':<16} {'STATUS':<12} {'ALOCADO':<10} {'MÁXIMO':<8} {'VEÍCULO'}")
    print(f"  {'─' * 87}")

    for ch in state["chargers"]:
        if ch["status"] == "OCUPADO":
            status_str = f"{C.GREEN}● OCUPADO{C.RESET}"
            allocated_str = f"{C.YELLOW}{ch['allocated_kw']:>5.1f} kW{C.RESET}"
            vehicle_str = ch["vehicle_id"] or ""
            duration_str = f"  {C.DIM}({ch['duration_min']:.0f} min | {ch['energy_kwh']:.2f} kWh){C.RESET}"
        else:
            status_str = f"{C.DIM}○ LIVRE{C.RESET}   "
            allocated_str = f"{C.DIM}{'—':>7}{C.RESET}"
            vehicle_str = ""
            duration_str = ""

        print(
            f"  {ch['id']:<8} {ch['location']:<16} {status_str:<20} "
            f"{allocated_str} {ch['max_kw']:>5.0f} kW    {vehicle_str}{duration_str}"
        )

    print(f"  {'─' * 87}\n")

    # ── Eventos recentes ──────────────────────────────────────────────
    print(f"{C.BOLD}  LOG DE EVENTOS RECENTES{C.RESET}")
    for ev in reversed(state.get("events", [])[-5:]):
        icon = {
            "VEHICLE_CONNECTED":    "🔌",
            "VEHICLE_DISCONNECTED": "🔋",
            "POWER_REDISTRIBUTED":  "⚡",
            "CHARGER_REGISTERED":   "🔧",
            "ERROR":                "❌",
        }.get(ev["type"], "•")
        print(f"  {C.DIM}{ev['timestamp']}{C.RESET}  {icon}  {ev['message']}")

    print(f"\n{C.DIM}{'─' * 88}{C.RESET}")


def render_session_summary(completed_sessions: list):
    """Exibe resumo das sessões concluídas."""
    if not completed_sessions:
        return

    print(f"\n{C.BOLD}  SESSÕES CONCLUÍDAS{C.RESET}")
    print(f"  {'─' * 87}")
    total_energy = 0.0
    for s in completed_sessions:
        total_energy += s.energy_consumed_kwh
        print(
            f"  {s.charger_id:<8} Veículo {s.vehicle_id:<12} "
            f"{s.duration_minutes():.1f} min   "
            f"{C.GREEN}{s.energy_consumed_kwh:.3f} kWh{C.RESET}"
        )
    print(f"  {'─' * 87}")
    print(f"  Total energia fornecida: {C.BOLD}{C.GREEN}{total_energy:.3f} kWh{C.RESET}\n")


def prompt_menu() -> str:
    """Exibe o menu de ações e retorna a escolha do usuário."""
    print(f"\n  {C.BOLD}AÇÕES:{C.RESET}")
    print(f"  [c] Conectar veículo")
    print(f"  [d] Desconectar veículo")
    print(f"  [s] Ver resumo de sessões")
    print(f"  [q] Sair\n")
    return input(f"  {C.CYAN}➤ Escolha: {C.RESET}").strip().lower()
