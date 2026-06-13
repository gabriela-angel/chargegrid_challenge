"""
ChargeGrid Intelligence — Simulação Interativa
----------------------------------------------
Ponto de entrada da aplicação.

Simula uma rede de eletropostos comerciais com controle de demanda
em tempo real. O operador pode conectar e desconectar veículos e
observar a redistribuição automática de potência.

"""

import sys
import time
import logging
from core.demand_controller import DemandController
from core.display import (
    render_dashboard,
    render_session_summary,
    prompt_menu,
    C,
)

logging.basicConfig(level=logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# Configuração do site comercial
# ──────────────────────────────────────────────────────────────────────────────

def setup_site() -> DemandController:
    """
    Configura o site comercial com 12 carregadores de 22 kW.
    Demanda total potencial: 12 × 22 = 264 kW.
    Limite contratado:       100 kW (limite útil: 95 kW).
    → Com todos ativos, cada carregador recebe ~7,92 kW (95 ÷ 12).
    Em produção: configuração carregada de banco de dados.
    """
    controller = DemandController(
        site_name="Shopping Paulista — Estacionamento",
        contracted_limit_kw=100.0,
        safety_margin_pct=0.05,  # limite útil = 95 kW
    )

    chargers = [
        ("CG-01", 22.0, "Vaga A1"), ("CG-02", 22.0, "Vaga A2"),
        ("CG-03", 22.0, "Vaga A3"), ("CG-04", 22.0, "Vaga A4"),
        ("CG-05", 22.0, "Vaga B1"), ("CG-06", 22.0, "Vaga B2"),
        ("CG-07", 22.0, "Vaga B3"), ("CG-08", 22.0, "Vaga B4"),
        ("CG-09", 22.0, "Vaga C1"), ("CG-10", 22.0, "Vaga C2"),
        ("CG-11", 22.0, "Vaga C3"), ("CG-12", 22.0, "Vaga C4"),
    ]

    for charger_id, max_kw, location in chargers:
        controller.register_charger(charger_id, max_kw, location)

    return controller


# ──────────────────────────────────────────────────────────────────────────────
# Fluxo interativo
# ──────────────────────────────────────────────────────────────────────────────

def handle_connect(controller: DemandController, state: dict) -> dict:
    free = [ch for ch in state["chargers"] if ch["status"] == "LIVRE"]
    if not free:
        print(f"\n  {C.RED}Todos os carregadores estão ocupados.{C.RESET}")
        time.sleep(2)
        return state

    print(f"\n  Carregadores livres: {', '.join(ch['id'] for ch in free)}")
    charger_id = input("  ID do carregador: ").strip().upper()
    vehicle_id = input("  ID do veículo (ex: ABC-1234): ").strip().upper()

    if not charger_id or not vehicle_id:
        print(f"  {C.RED}Entrada inválida.{C.RESET}")
        time.sleep(1.5)
        return state

    try:
        result = controller.vehicle_connect(charger_id, vehicle_id)
        if "error" in result:
            print(f"\n  {C.RED}Erro: {result['error']}{C.RESET}")
            time.sleep(2)
        else:
            print(f"\n  {C.GREEN}✓ Veículo conectado! Potência redistribuída.{C.RESET}")
            time.sleep(1.5)
            return result
    except ValueError as e:
        print(f"\n  {C.RED}Erro: {e}{C.RESET}")
        time.sleep(2)

    return state


def handle_disconnect(controller: DemandController, state: dict) -> dict:
    occupied = [ch for ch in state["chargers"] if ch["status"] == "OCUPADO"]
    if not occupied:
        print(f"\n  {C.YELLOW}Nenhum carregador está ocupado.{C.RESET}")
        time.sleep(2)
        return state

    print(f"\n  Carregadores ocupados: {', '.join(ch['id'] for ch in occupied)}")
    charger_id = input("  ID do carregador: ").strip().upper()

    try:
        result = controller.vehicle_disconnect(charger_id)
        if "error" in result:
            print(f"\n  {C.RED}Erro: {result['error']}{C.RESET}")
            time.sleep(2)
        else:
            print(f"\n  {C.GREEN}✓ Veículo desconectado! Potência liberada e redistribuída.{C.RESET}")
            time.sleep(1.5)
            return result
    except ValueError as e:
        print(f"\n  {C.RED}Erro: {e}{C.RESET}")
        time.sleep(2)

    return state


# ──────────────────────────────────────────────────────────────────────────────
# Demo automática — cenário de congestionamento e alívio
# ──────────────────────────────────────────────────────────────────────────────

def run_demo(controller: DemandController):
    """
    Demonstração automática com 12 carregadores de 22 kW e limite de 95 kW.

    Narrativa do cenário:
      Fase 1 — Chegada gradual: carros vão conectando, potência por carregador
               vai caindo conforme o orçamento é dividido entre mais sessões.
      Fase 2 — Congestionamento: todos os 12 carregadores ativos simultaneamente.
               Cada um recebe apenas ~7,92 kW (95 ÷ 12) — bem abaixo dos 22 kW
               do hardware, mas o shopping permanece dentro do limite contratado.
      Fase 3 — Alívio: carros vão desconectando, potência por carregador sobe
               automaticamente até que os últimos recebam os 22 kW completos.
    """
    pause = 2.5

    def step(action, charger_id, vehicle_id=None, label=None):
        if action == "connect":
            state = controller.vehicle_connect(charger_id, vehicle_id)
        else:
            state = controller.vehicle_disconnect(charger_id)
        render_dashboard(state)
        render_session_summary(controller.completed_sessions)
        msg = label or (f"Conectando {vehicle_id} → {charger_id}" if action == "connect"
                        else f"Desconectando {charger_id}")
        print(f"\n  {C.DIM}[DEMO] {msg} — próximo evento em {pause}s...{C.RESET}")
        time.sleep(pause)
        return state

    print(f"\n  {C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════╗")
    print(f"  ║   DEMO: 12 carregadores × 22 kW | Limite: 100 kW     ║")
    print(f"  ║   Demanda potencial: 264 kW  →  limite útil: 95 kW   ║")
    print(f"  ╚══════════════════════════════════════════════════════╝{C.RESET}\n")
    time.sleep(3)

    # ── Fase 1: Chegada gradual ───────────────────────────────────────
    print(f"  {C.YELLOW}▶ FASE 1 — Chegada gradual de veículos{C.RESET}")
    time.sleep(1.5)

    connects_phase1 = [
        ("CG-01", "CAR-0001"), ("CG-02", "CAR-0002"), ("CG-03", "CAR-0003"),
        ("CG-04", "CAR-0004"), ("CG-05", "CAR-0005"), ("CG-06", "CAR-0006"),
    ]
    for cg, car in connects_phase1:
        step("connect", cg, car)

    # ── Fase 2: Congestionamento total ───────────────────────────────
    print(f"  {C.RED}{C.BOLD}▶ FASE 2 — Congestionamento: todos os 12 carregadores ativos{C.RESET}")
    time.sleep(1.5)

    connects_phase2 = [
        ("CG-07", "CAR-0007"), ("CG-08", "CAR-0008"), ("CG-09", "CAR-0009"),
        ("CG-10", "CAR-0010"), ("CG-11", "CAR-0011"), ("CG-12", "CAR-0012"),
    ]
    for cg, car in connects_phase2:
        step("connect", cg, car)

    # Pausa extra para o momento de máxima tensão
    state = controller._state_snapshot()
    render_dashboard(state)
    kw_each = state["chargers"][0]["allocated_kw"]
    print(f"\n  {C.RED}{C.BOLD}  ⚡ 12 carros ativos | {kw_each} kW por carregador | "
          f"Total: {state['total_allocated_kw']} kW / {state['usable_limit_kw']} kW{C.RESET}")
    print(f"  {C.DIM}  O shopping está no limite — nenhum carro sem carga, nenhuma multa.{C.RESET}")
    time.sleep(4)

    # ── Fase 3: Alívio gradual ────────────────────────────────────────
    print(f"  {C.GREEN}{C.BOLD}▶ FASE 3 — Alívio: carros concluem e desconectam{C.RESET}")
    time.sleep(1.5)

    disconnects = [
        "CG-01", "CG-02", "CG-03", "CG-04",
        "CG-05", "CG-06", "CG-07", "CG-08",
        "CG-09",
    ]
    for cg in disconnects:
        step("disconnect", cg)

    # Estado final
    state = controller._state_snapshot()
    render_dashboard(state)
    render_session_summary(controller.completed_sessions)
    print(f"\n  {C.GREEN}{C.BOLD}✓ Demonstração concluída.{C.RESET}")
    print(f"  {C.DIM}  3 carros restantes recebem 22 kW cada (teto do hardware).{C.RESET}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Ponto de entrada
# ──────────────────────────────────────────────────────────────────────────────

def main():
    controller = setup_site()
    state = controller._state_snapshot()

    if "--demo" in sys.argv:
        print(f"\n  {C.CYAN}Iniciando demonstração em 2s...{C.RESET}")
        time.sleep(2)
        run_demo(controller)
        return

    while True:
        render_dashboard(state)
        choice = prompt_menu()

        if choice == "c":
            state = handle_connect(controller, state)
        elif choice == "d":
            state = handle_disconnect(controller, state)
        elif choice == "s":
            render_dashboard(state)
            render_session_summary(controller.completed_sessions)
            input(f"\n  {C.DIM}Pressione Enter para continuar...{C.RESET}")
        elif choice == "q":
            render_dashboard(state)
            render_session_summary(controller.completed_sessions)
            print(f"\n  {C.CYAN}ChargeGrid encerrado. Obrigado!{C.RESET}\n")
            break
        else:
            print(f"  {C.RED}Opção inválida.{C.RESET}")
            time.sleep(1)


if __name__ == "__main__":
    main()