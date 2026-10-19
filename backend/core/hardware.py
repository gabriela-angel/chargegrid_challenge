"""
ChargeGrid Intelligence — Ligação com o hardware via Modbus TCP
-----------------------------------------------------------------
Camada fina entre o DemandController e os carregadores GoodWe HCA G2. Fala
Modbus TCP com um equipamento real OU com o simulador (simulator/hca_g2_sim.py):
para o ChargeGrid é indiferente, só muda HARDWARE_HOST/HARDWARE_PORT.

Cada carregador é um "escravo" Modbus: CG-01 = unit 1, CG-02 = unit 2, ...

Registradores usados (ver core/modbus_map.py):
    10029  escrita  limite máximo de potência (kW x 10)   <- redistribuição de carga
    10060  escrita  1 = desligar | 2 = ligar carregamento
    10015  leitura  potência atual (kW x 10)               <- conferência do que foi aplicado

Falhas de rede não podem derrubar o controle de demanda. Se o carregador não
responde, a chamada levanta HardwareError e o link entra em "pausa" (cooldown):
nas próximas chamadas falha na hora, sem esperar o timeout de novo. O controller
captura o erro, registra um evento e segue funcionando com a lógica interna.

Configuração por variáveis de ambiente (ver HardwareLink.from_env):
    HARDWARE_MODE   "off" (padrão) | "modbus"
    HARDWARE_HOST   padrão 127.0.0.1
    HARDWARE_PORT   padrão 5020 (a 502 exige root; o simulador usa 5020)
"""

import logging
import os
import threading
import time
from typing import Callable, Optional

from core.modbus_map import HCA_G2, power_to_register, register_to_power

logger = logging.getLogger(__name__)

REG_ON, REG_OFF = 2, 1  # valores do registrador 10060


class HardwareError(Exception):
    """Falha ao falar com o carregador (rede fora, timeout ou Modbus recusou)."""


class HardwareLink:
    def __init__(self, host: str = "127.0.0.1", port: int = 5020,
                 timeout: float = 0.5, cooldown_s: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.cooldown_s = cooldown_s
        self._client = None
        self._lock = threading.Lock()      # o cliente Modbus não é thread-safe
        self._down_until = 0.0             # enquanto agora < isto, falha sem tentar
        self.writes = 0
        self.reads = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Criação a partir do ambiente
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> Optional["HardwareLink"]:
        """Cria o link conforme as variáveis de ambiente; None se HARDWARE_MODE != modbus."""
        if os.getenv("HARDWARE_MODE", "off").strip().lower() != "modbus":
            return None
        return cls(
            host=os.getenv("HARDWARE_HOST", "127.0.0.1"),
            port=int(os.getenv("HARDWARE_PORT", "5020")),
        )

    @staticmethod
    def unit_id(charger_id: str) -> int:
        """'CG-05' -> 5. O número do carregador é o endereço (unit id) no barramento."""
        try:
            return int(charger_id.split("-")[-1])
        except ValueError as exc:
            raise HardwareError(f"id de carregador inválido para Modbus: {charger_id}") from exc

    # ------------------------------------------------------------------
    # Núcleo: executa uma operação Modbus com tratamento de falhas
    # ------------------------------------------------------------------
    def _call(self, operation: Callable):
        """
        Roda `operation(client)` e devolve a resposta Modbus.
        - Rede/timeout: fecha a conexão, abre o cooldown e levanta HardwareError.
        - Resposta de erro do carregador (ex.: endereço ilegal): levanta HardwareError
          SEM abrir cooldown, porque o equipamento está vivo — só recusou o pedido.
        """
        with self._lock:
            if time.monotonic() < self._down_until:
                raise HardwareError(
                    f"hardware {self.host}:{self.port} indisponível (em pausa após falha)")
            try:
                if self._client is None:
                    from pymodbus.client import ModbusTcpClient
                    self._client = ModbusTcpClient(
                        self.host, port=self.port, timeout=self.timeout, retries=0)
                if not self._client.connected and not self._client.connect():
                    raise ConnectionError(f"sem conexão com {self.host}:{self.port}")
                response = operation(self._client)
            except Exception as exc:  # rede, timeout, pymodbus
                self._trip(f"{type(exc).__name__}: {exc}")
                raise HardwareError(self.last_error) from exc
            if response.isError():
                self.errors += 1
                self.last_error = f"Modbus recusou o pedido: {response}"
                raise HardwareError(self.last_error)
            self.last_error = None
            return response

    def _trip(self, message: str):
        """Registra a falha, derruba a conexão e inicia o cooldown."""
        self.errors += 1
        self.last_error = message
        self._down_until = time.monotonic() + self.cooldown_s
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Operações de alto nível usadas pelo DemandController
    # ------------------------------------------------------------------
    def write_limit(self, charger_id: str, kw: float) -> float:
        """
        Escreve o limite de potência no registrador 10029 e devolve o valor que o
        carregador REALMENTE aplicou (ele ajusta à faixa do modelo, ex.: 4,2–22 kW).
        """
        unit = self.unit_id(charger_id)
        response = self._call(lambda c: c.write_register(
            HCA_G2.WRITE.MAX_CHARGING_POWER, power_to_register(kw), device_id=unit))
        self.writes += 1
        return register_to_power(response.registers[0])

    def set_charging(self, charger_id: str, on: bool) -> None:
        """Liga (2) ou desliga (1) o carregamento pelo registrador 10060."""
        unit = self.unit_id(charger_id)
        self._call(lambda c: c.write_register(
            HCA_G2.WRITE.CHARGER_ON_OFF, REG_ON if on else REG_OFF, device_id=unit))
        self.writes += 1

    def read_power(self, charger_id: str) -> float:
        """Lê a potência atual (registrador 10015) em kW."""
        return self._read_kw(charger_id, HCA_G2.READ.CHARGING_POWER)

    def read_limit(self, charger_id: str) -> float:
        """Lê de volta o limite gravado (registrador 10029) em kW."""
        return self._read_kw(charger_id, HCA_G2.WRITE.MAX_CHARGING_POWER)

    def _read_kw(self, charger_id: str, address: int) -> float:
        unit = self.unit_id(charger_id)
        response = self._call(lambda c: c.read_holding_registers(address, count=1, device_id=unit))
        self.reads += 1
        return register_to_power(response.registers[0])

    # ------------------------------------------------------------------
    # Estado e encerramento
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """Resumo para o endpoint /api/hardware e para o painel."""
        return {
            "mode": "modbus",
            "host": self.host,
            "port": self.port,
            "connected": bool(self._client is not None and self._client.connected),
            "paused": time.monotonic() < self._down_until,
            "writes": self.writes,
            "reads": self.reads,
            "errors": self.errors,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
