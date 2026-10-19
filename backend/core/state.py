"""
ChargeGrid Intelligence — Estado da Aplicação
----------------------------------------------
Ponto único de acesso ao DemandController.

Sprint 2 (CLI): o main.py antigo criava o controller localmente.
Sprint 3 (API): o FastAPI precisa de UMA instância compartilhada entre
todas as requisições (todo mundo conecta/desconecta no mesmo "site").

Isso é o dicionário/objeto global em memória RAM mencionado no plano:
o controller inteiro VIVE aqui enquanto o processo do uvicorn roda.
Se o servidor reiniciar, o estado (sessões ativas) se perde — é
esperado nessa fase. Persistência de histórico fica pro Postgres
(Transactions), não pro estado em tempo real.
"""

from datetime import datetime

from core.demand_controller import DemandController, DEFAULT_SAFETY_MARGIN_PCT
from core.hardware import HardwareLink
from ai.predictor import predict_hour

# CG-01 é o carregador "físico": o ESP32 vai chamar os endpoints
# /api/chargers/CG-01/connect e /disconnect via WiFi.
# CG-02 a CG-12 são 100% simulados — controlados só pela API
# (Postman, o dashboard do operador, ou um script de simulação).
CHARGERS = [
    ("CG-01", 22.0, "Vaga A1 (ESP32 físico)"),
    ("CG-02", 22.0, "Vaga A2"), ("CG-03", 22.0, "Vaga A3"), ("CG-04", 22.0, "Vaga A4"),
    ("CG-05", 22.0, "Vaga B1"), ("CG-06", 22.0, "Vaga B2"), ("CG-07", 22.0, "Vaga B3"),
    ("CG-08", 22.0, "Vaga B4"), ("CG-09", 22.0, "Vaga C1"), ("CG-10", 22.0, "Vaga C2"),
    ("CG-11", 22.0, "Vaga C3"), ("CG-12", 22.0, "Vaga C4"),
]


def build_controller() -> DemandController:
    controller = DemandController(
        site_name="Shopping Paulista — Estacionamento",
        contracted_limit_kw=100.0,
        # Limite de uso padrão de 90% (margem de 10%); ajustável em runtime
        # pelo dashboard via PUT /api/settings.
        safety_margin_pct=DEFAULT_SAFETY_MARGIN_PCT,
    )
    for charger_id, max_kw, location in CHARGERS:
        controller.register_charger(charger_id, max_kw, location)

    # A tarifa dinâmica usa a previsão da IA como "pressão esperada" da rede.
    # Se o modelo ainda não foi treinado, o controller cai para a pressão ao vivo.
    def forecast_kw(when: datetime) -> float:
        try:
            return predict_hour(when.hour, when.weekday())
        except FileNotFoundError:
            return 0.0

    controller.forecast_provider = forecast_kw
    controller.calibrate_tariff_pressure()   # limiares da tarifa vêm da previsão da IA (se houver modelo)

    # Hardware via Modbus TCP: só ligado com HARDWARE_MODE=modbus (ver core/hardware.py).
    # Aponta para o simulador (python -m simulator.hca_g2_sim) ou para o HCA G2 real.
    controller.hardware = HardwareLink.from_env()
    return controller


# Instância única, importada pelos routers via `from core.state import controller`.
controller = build_controller()
