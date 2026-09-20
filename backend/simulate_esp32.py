"""
ChargeGrid Intelligence — Simulador de ESP32
------------------------------------------------
Este script FINGE ser o ESP32 físico, usando HTTP puro (biblioteca
`requests`) em vez de WiFi + C++. A ideia é dupla:

  1. Vocês conseguem testar e demonstrar o fluxo completo (conectar →
     potência sendo redistribuída → energia subindo → desconectar →
     resumo da sessão) HOJE, sem esperar o hardware chegar.

  2. Quando o ESP32 físico chegar, o firmware C++ vai fazer
     EXATAMENTE essas 3 chamadas (setup(), loop() com polling,
     e o evento de desconexão) — é o roteiro/pseudocódigo direto
     pra depois. Só troca `requests.post(...)` por `HTTPClient` do
     Arduino.

Como rodar (com o backend já rodando em outro terminal):
    pip install requests
    python simulate_esp32.py
"""

import time
import requests

BASE_URL = "http://127.0.0.1:8000/api/chargers"
CHARGER_ID = "CG-01"   # o carregador "físico"
VEHICLE_ID = "CAR-ESP32"
POLL_INTERVAL_SECONDS = 3
CHARGE_DURATION_SECONDS = 30  # simula quanto tempo o "carro" fica plugado


def connect():
    print(f"[ESP32] Botão pressionado → conectando {VEHICLE_ID} em {CHARGER_ID}...")
    resp = requests.post(f"{BASE_URL}/{CHARGER_ID}/connect", json={"vehicle_id": VEHICLE_ID})
    resp.raise_for_status()
    charger = resp.json()
    print(f"[ESP32] Conectado. Potência inicial alocada: {charger['allocated_kw']} kW")


def poll_allocation():
    """
    Isso é o `loop()` do firmware: a cada N segundos, pergunta pro
    backend "quanto eu posso puxar agora?" — porque outro carregador
    pode ter conectado/desconectado e mudado a redistribuição.

    No ESP32 real, o valor de allocated_kw controlaria, por exemplo,
    a frequência de piscar de um LED (quanto mais kW, mais rápido).
    """
    resp = requests.get(f"{BASE_URL}/{CHARGER_ID}")
    resp.raise_for_status()
    charger = resp.json()
    print(
        f"[ESP32] poll → status={charger['status']} | "
        f"alocado={charger['allocated_kw']} kW | "
        f"energia acumulada={charger['energy_kwh']} kWh"
    )
    return charger


def disconnect():
    print(f"[ESP32] Botão pressionado novamente → desconectando {CHARGER_ID}...")
    resp = requests.post(f"{BASE_URL}/{CHARGER_ID}/disconnect")
    resp.raise_for_status()
    result = resp.json()
    print(
        f"[ESP32] Sessão encerrada. "
        f"Energia total: {result['energy_kwh']} kWh | "
        f"Duração: {result['duration_min']} min"
    )


def main():
    connect()

    elapsed = 0
    while elapsed < CHARGE_DURATION_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
        poll_allocation()

    disconnect()


if __name__ == "__main__":
    main()
