"""
ChargeGrid Intelligence — Simulador Modbus TCP do carregador GoodWe HCA G2
---------------------------------------------------------------------------
Faz o papel dos carregadores físicos enquanto o hardware não existe. Fala o
MESMO protocolo e usa o MESMO mapa de registradores (core/modbus_map.py), então
o ChargeGrid não sabe a diferença: trocar o simulador pelo carregador real é
só apontar HARDWARE_HOST/HARDWARE_PORT para o IP do equipamento.

Cada carregador é um "escravo" Modbus (unit id 1..N: CG-01 = 1, CG-02 = 2...).

Comportamento reproduzido (subconjunto do HCA G2 que o controle de demanda usa):
  10029  RW  limite máx. de potência (kW x 10). Fica dentro da faixa do modelo
             (22 kW: 4,2 a 22,0 kW); fora dela o valor é ajustado, como no HCA G2.
  10060  RW  1 = desligar carga | 2 = ligar carga
  10015  RO  potência atual (kW x 10) = limite, se estiver carregando; senão 0
  10016  RO  energia da sessão (kWh x 10), integrada no tempo
  10017  RO  estado (0 livre | 3 carregando)
  10075  RO  carro conectado? (0 = não | 2 = sim)
  10108  RO  fonte de energia (bit0 = rede)
Escrever em registrador somente-leitura devolve "endereço ilegal".

Como rodar (a porta 502 exige root; usamos 5020):
    python -m simulator.hca_g2_sim --port 5020 --chargers 12
"""

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from core.modbus_map import HCA_G2, ChargerStatus

logger = logging.getLogger("hca_g2_sim")

WRITABLE = {
    HCA_G2.WRITE.MAX_CHARGING_POWER,   # 10029
    HCA_G2.WRITE.CHARGER_ON_OFF,       # 10060
    HCA_G2.WRITE.EMS_DISPATCH,         # 10000
    HCA_G2.WRITE.DLM_ENABLE,           # 10025
}
REG_ON, REG_OFF = 2, 1                 # valores do registrador 10060
REGISTER_SPACE = 10200                 # endereços 0..10199 existem


@dataclass
class SimCharger:
    """Estado interno de um carregador simulado (o que o 'firmware' sabe)."""
    unit_id: int
    rated_kw: float = 22.0
    min_kw: float = 4.2
    limit_kw: float = 22.0             # registrador 10029
    charging: bool = False
    energy_kwh: float = 0.0
    writes: int = 0                    # contador, útil para testes e demo
    _last_t: float = field(default_factory=time.monotonic)

    # --- física simplificada ---------------------------------------
    def advance(self, now: Optional[float] = None) -> None:
        """Integra a energia desde a última chamada (kWh = kW x horas)."""
        now = time.monotonic() if now is None else now
        if self.charging:
            self.energy_kwh += self.power_kw * (now - self._last_t) / 3600.0
        self._last_t = now

    @property
    def power_kw(self) -> float:
        return self.limit_kw if self.charging else 0.0

    # --- escritas vindas do ChargeGrid ------------------------------
    def write_limit(self, raw: int) -> int:
        """10029: devolve o valor efetivamente aplicado (ajustado à faixa do modelo)."""
        self.advance()
        kw = max(self.min_kw, min(self.rated_kw, raw / 10.0))
        self.limit_kw = kw
        self.writes += 1
        return int(round(kw * 10))

    def write_on_off(self, raw: int) -> bool:
        """10060: 2 liga, 1 desliga. Valor diferente disso é inválido."""
        if raw not in (REG_ON, REG_OFF):
            return False
        self.advance()
        self.charging = raw == REG_ON
        if self.charging:
            self.energy_kwh = 0.0          # nova sessão; ao desligar, o total fica lido até a próxima
        return True

    # --- leituras ----------------------------------------------------
    def read(self, address: int) -> Optional[int]:
        """Valor atual de um registrador, ou None se este simulador não o modela."""
        self.advance()
        if address == HCA_G2.READ.CHARGING_POWER:
            return int(round(self.power_kw * 10))
        if address == HCA_G2.READ.CHARGING_CAPACITY:
            return int(round(self.energy_kwh * 10))
        if address == HCA_G2.READ.CHARGER_STATUS:
            return ChargerStatus.CHARGING if self.charging else ChargerStatus.IDLE_NO_GUN
        if address == HCA_G2.READ.CAR_CONN_STATUS:
            return 2 if self.charging else 0
        if address == HCA_G2.READ.CHARGING_POWER_SOURCE:
            return 0b001                   # rede (o simulador não modela PV/bateria)
        if address == HCA_G2.WRITE.MAX_CHARGING_POWER:
            return int(round(self.limit_kw * 10))
        if address == HCA_G2.WRITE.CHARGER_ON_OFF:
            return REG_ON if self.charging else REG_OFF
        return None


class ChargerBank:
    """Conjunto de carregadores simulados, endereçados por unit id."""

    def __init__(self, count: int = 12, rated_kw: float = 22.0):
        self.chargers = {i: SimCharger(unit_id=i, rated_kw=rated_kw) for i in range(1, count + 1)}


def make_action(charger: SimCharger):
    """
    Ganchos do pymodbus: roda a cada leitura/escrita deste escravo.
    - Escrita: aplica a regra do HCA G2 e ajusta o valor realmente gravado.
    - Leitura: sobrescreve a resposta com o estado vivo do carregador.
    """
    from pymodbus.constants import ExcCodes

    async def action(function_code, start_address, address, count,
                     current_registers, set_values):
        if set_values is not None:  # escrita
            for offset, raw in enumerate(set_values):
                reg = address + offset
                if reg not in WRITABLE:
                    logger.warning("[unit %s] escrita recusada em registrador RO %s", charger.unit_id, reg)
                    return ExcCodes.ILLEGAL_ADDRESS
                if reg == HCA_G2.WRITE.MAX_CHARGING_POWER:
                    applied = charger.write_limit(int(raw))
                    set_values[offset] = applied
                    logger.info("[unit %s] 10029 <- %s (aplicado %.1f kW)",
                                charger.unit_id, raw, applied / 10)
                elif reg == HCA_G2.WRITE.CHARGER_ON_OFF:
                    if not charger.write_on_off(int(raw)):
                        return ExcCodes.ILLEGAL_VALUE
                    logger.info("[unit %s] 10060 <- %s (%s)", charger.unit_id, raw,
                                "LIGAR" if raw == REG_ON else "DESLIGAR")
            return None
        # leitura
        for offset in range(count):
            value = charger.read(address + offset)
            if value is not None:
                idx = (address + offset) - start_address
                if 0 <= idx < len(current_registers):
                    current_registers[idx] = value
        return None

    return action


def build_devices(bank: ChargerBank):
    from pymodbus.simulator import DataType, SimData, SimDevice
    devices = []
    for unit_id, charger in bank.chargers.items():
        devices.append(SimDevice(
            id=unit_id,
            simdata=[SimData(address=0, count=REGISTER_SPACE, values=0, datatype=DataType.REGISTERS)],
            action=make_action(charger),
        ))
    return devices


async def serve(host: str, port: int, count: int, bank: Optional[ChargerBank] = None):
    from pymodbus.server import StartAsyncTcpServer
    bank = bank or ChargerBank(count)
    logger.info("Simulador HCA G2 em %s:%s com %s carregadores (unit 1..%s)", host, port, count, count)
    await StartAsyncTcpServer(context=build_devices(bank), address=(host, port))


def main():
    ap = argparse.ArgumentParser(description="Simulador Modbus TCP do GoodWe HCA G2")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--chargers", type=int, default=12)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    asyncio.run(serve(args.host, args.port, args.chargers))


if __name__ == "__main__":
    main()
