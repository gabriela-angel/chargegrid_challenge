"""
Testes da integração Modbus: simulador do HCA G2, HardwareLink e a ligação com o
DemandController. O simulador sobe de verdade (Modbus TCP em porta livre, numa
thread), então o teste atravessa a rede como no uso real.
Rodar de dentro de backend/:  python -m pytest -q
"""
import asyncio
import socket
import threading
import time

import pytest

from core.demand_controller import DemandController
from core.hardware import HardwareError, HardwareLink
from simulator.hca_g2_sim import SimCharger, serve


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def simulator():
    """Sobe o simulador (3 carregadores) numa thread e devolve (porta, banco de carregadores)."""
    from simulator.hca_g2_sim import ChargerBank
    port = free_port()
    bank = ChargerBank(3)
    threading.Thread(
        target=lambda: asyncio.run(serve("127.0.0.1", port, 3, bank)), daemon=True).start()
    for _ in range(50):                      # espera o servidor aceitar conexões
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return port, bank


def make_controller(port: int) -> DemandController:
    c = DemandController("Teste", 100.0)
    for i in range(1, 4):
        c.register_charger(f"CG-{i:02d}", 22.0, f"Vaga {i}")
    c.pv_peak_kw = 0.0
    c.hardware = HardwareLink("127.0.0.1", port, timeout=0.5, cooldown_s=0.3)
    return c


# ---------------- carregador simulado (sem rede) ----------------
def test_limite_fora_da_faixa_e_ajustado():
    ch = SimCharger(unit_id=1)
    assert ch.write_limit(900) == 220        # 90 kW pedidos -> teto de 22,0 kW
    assert ch.write_limit(10) == 42          # 1 kW pedido -> piso de 4,2 kW


def test_energia_e_integrada_no_tempo():
    ch = SimCharger(unit_id=1)
    ch.write_limit(100)                      # 10 kW
    ch.write_on_off(2)
    t0 = ch._last_t
    ch.advance(t0 + 360)                     # 6 min a 10 kW = 1 kWh
    assert ch.energy_kwh == pytest.approx(1.0)
    ch._last_t = time.monotonic()            # read() usa o relógio real; realinha o falso
    assert ch.read(10016) == 10              # registrador em kWh x 10


def test_valor_invalido_no_liga_desliga():
    assert SimCharger(unit_id=1).write_on_off(7) is False


# ---------------- HardwareLink x simulador ----------------
def test_link_escreve_e_le_de_volta(simulator):
    port, _ = simulator
    link = HardwareLink("127.0.0.1", port)
    assert link.write_limit("CG-02", 15.5) == 15.5
    link.set_charging("CG-02", True)
    assert link.read_limit("CG-02") == 15.5
    assert link.read_power("CG-02") == 15.5   # carregando: potência = limite
    link.set_charging("CG-02", False)
    assert link.read_power("CG-02") == 0.0
    link.close()


def test_link_informa_valor_aplicado_quando_ajustado(simulator):
    port, _ = simulator
    link = HardwareLink("127.0.0.1", port)
    assert link.write_limit("CG-03", 90.0) == 22.0
    link.close()


def test_registrador_somente_leitura_e_recusado_sem_abrir_pausa(simulator):
    port, _ = simulator
    link = HardwareLink("127.0.0.1", port)
    with pytest.raises(HardwareError):
        link._call(lambda c: c.write_register(10015, 5, device_id=1))
    assert link.status()["paused"] is False  # o equipamento está vivo, só recusou
    link.close()


# ---------------- DemandController x simulador ----------------
def test_conectar_veiculo_grava_limite_e_liga_a_carga(simulator):
    port, bank = simulator
    c = make_controller(port)
    c.vehicle_connect("CG-01", "ABC1D23")
    ch = bank.chargers[1]
    assert ch.charging is True
    assert ch.limit_kw == 22.0
    assert c._read_power_from_hardware("CG-01") == 22.0
    c.vehicle_disconnect("CG-01")
    assert ch.charging is False
    assert c.hardware.read_power("CG-01") == 0.0     # leitura direta no equipamento
    types = [e["type"] for e in c.events]
    assert types.count("MODBUS_WRITE") >= 3  # limite + ligar + desligar


def test_redistribuicao_chega_ao_hardware(simulator):
    """Orçamento apertado: ao entrar o 2º carro o limite do 1º cai no carregador."""
    port, bank = simulator
    c = make_controller(port)
    c.contracted_limit_kw = 40.0              # útil 36 kW
    c.vehicle_connect("CG-01", "A")
    assert bank.chargers[1].limit_kw == 22.0
    c.vehicle_connect("CG-02", "B")           # 36 / 2 = 18 kW cada
    assert bank.chargers[1].limit_kw == 18.0
    assert bank.chargers[2].limit_kw == 18.0
    c.vehicle_disconnect("CG-01")
    c.vehicle_disconnect("CG-02")


def test_so_escreve_quando_o_valor_muda(simulator):
    port, bank = simulator
    c = make_controller(port)
    c.vehicle_connect("CG-01", "A")
    writes = c.hardware.writes
    c._redistribute_power(log=False)          # nada mudou -> nenhuma escrita nova
    assert c.hardware.writes == writes
    c.vehicle_disconnect("CG-01")


# ---------------- falhas de rede ----------------
def test_hardware_fora_do_ar_nao_derruba_o_controle():
    c = make_controller(free_port())          # ninguém escuta nesta porta
    snap = c.vehicle_connect("CG-01", "A")    # não pode levantar exceção
    assert "error" not in snap
    assert c.chargers["CG-01"].current_kw == 22.0
    errors = [e for e in c.events if e["type"] == "MODBUS_ERROR"]
    assert len(errors) == 1                   # avisa uma vez, não a cada tentativa
    assert c.hardware.status()["paused"] is True
    assert c._read_power_from_hardware("CG-01") == 22.0   # fallback: potência alocada


def test_recupera_quando_o_hardware_volta(simulator):
    port, bank = simulator
    c = make_controller(port)
    c.hardware.port = free_port()             # aponta para o vazio: falha
    c.vehicle_connect("CG-01", "A")
    assert bank.chargers[1].charging is False
    c.hardware.port = port                    # o hardware "volta"
    c.hardware.close()
    time.sleep(0.4)                           # passa o cooldown
    c._redistribute_power(log=False)          # próxima sincronização repõe o estado
    assert bank.chargers[1].charging is True
    assert any(e["type"] == "MODBUS_RECOVERED" for e in c.events)
    c.vehicle_disconnect("CG-01")
