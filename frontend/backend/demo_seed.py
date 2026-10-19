"""
ChargeGrid Intelligence — Dados da demonstração (clientes fictícios)
----------------------------------------------------------------------
Antes, a demo ocupava vagas com placas soltas/aleatórias — ninguém estava
cadastrado, então o dashboard não tinha clientes, o histórico não tinha dono
e o "fluxo do cliente" era contornado. Agora a demo roda com CLIENTES
FICTÍCIOS JÁ CADASTRADOS: cada um tem conta (login com CPF + senha), veículos
salvos e recargas anteriores no histórico. As recargas ao vivo da demo
(scripts/demo_scenario.py, scripts/seed_demo.py) passam pela MESMA camada do
app do cliente (meta de recarga, "minha recarga", recibo), só pulando o
pagamento.

Este módulo é só DADO + funções de povoamento — não tem rota nem estado próprio:
usa client_store.py (contas) e o backend real (histórico, via
RemoteController.import_sessions). Ligado por padrão; DEMO_MODE=0 desliga tudo
(clientes, histórico e as rotas /demo/* do main.py).

Todos os nomes, CPFs (válidos só no dígito verificador), e-mails (example.com)
e placas são inventados.
"""

import math
import random
from datetime import datetime, timedelta

import client_store

DEMO_PASSWORD = "demo123"  # mesma senha do login de empresa de demonstração


def _cpf_from_base(base9: str) -> str:
    """Completa 9 dígitos com os 2 dígitos verificadores de CPF (11 dígitos, sem máscara)."""
    digits = [int(c) for c in base9]
    for size in (9, 10):
        total = sum(d * w for d, w in zip(digits, range(size + 1, 1, -1)))
        digits.append((total * 10 % 11) % 10)
    return "".join(str(d) for d in digits)


# (nome, e-mail, base do CPF, [(modelo, placa), ...])
# 12 clientes = 12 vagas: dá pra lotar o site com uma pessoa diferente em cada uma.
_CLIENTS = [
    ("Mariana Costa",    "mariana.costa@example.com",    "390533447", [("BYD Dolphin", "RKB-4E21")]),
    ("Rafael Mendes",    "rafael.mendes@example.com",    "123456789", [("BYD Dolphin Mini", "QWE-7F14"), ("Renault Kwid E-Tech", "LPT-2A63")]),
    ("Camila Duarte",    "camila.duarte@example.com",    "246813579", [("Volvo EX30", "SDM-5G82")]),
    ("Felipe Andrade",   "felipe.andrade@example.com",   "135792468", [("BYD Seal", "TRX-9H37")]),
    ("Beatriz Nogueira", "beatriz.nogueira@example.com", "864209753", [("GWM Ora 03", "JKL-1C45")]),
    ("Lucas Ferraz",     "lucas.ferraz@example.com",     "975318642", [("Peugeot e-208", "MNB-6D90")]),
    ("Juliana Prado",    "juliana.prado@example.com",    "412578963", [("BYD Yuan Plus", "VCX-3B76")]),
    ("Thiago Bastos",    "thiago.bastos@example.com",    "798415263", [("Volvo XC40 Recharge", "ZAQ-8E18")]),
    ("Larissa Moreira",  "larissa.moreira@example.com",  "357159486", [("Fiat 500e", "PLM-4F29"), ("JAC E-JS1", "OKN-0G53")]),
    ("Gustavo Ribeiro",  "gustavo.ribeiro@example.com",  "681947352", [("Chevrolet Bolt EUV", "IJN-2H64")]),
    ("Patrícia Lopes",   "patricia.lopes@example.com",   "529174836", [("BYD Dolphin", "UHB-7A08")]),
    ("Diego Carvalho",   "diego.carvalho@example.com",   "743256189", [("Renault Kwid E-Tech", "YGV-5C31")]),
]

DEMO_CLIENTS = [
    {"nome": nome, "email": email, "cpf": _cpf_from_base(base), "veiculos": [{"modelo": m, "placa": p} for m, p in veiculos]}
    for nome, email, base, veiculos in _CLIENTS
]

# Recargas que os clientes "já fizeram hoje" (histórico inicial).
# (índice do cliente, índice do veículo, vaga, terminou há N min, duração em min, potência média em kW)
# Determinístico de propósito (nada de random): a demo conta sempre a mesma história.
# Fora CG-01 — é o carregador físico (ESP32), não faz sentido ter histórico "simulado" nele.
_HISTORY = [
    (0, 0, "CG-02",  22, 48, 11.8), (1, 0, "CG-05",  41, 35, 14.6), (2, 0, "CG-03",  58, 62,  9.4),
    (3, 0, "CG-07",  75, 28, 19.7), (4, 0, "CG-04",  96, 55, 10.2), (5, 0, "CG-09", 118, 40, 13.1),
    (6, 0, "CG-06", 140, 72,  8.9), (7, 0, "CG-11", 165, 33, 17.5), (8, 1, "CG-08", 190, 50, 12.4),
    (9, 0, "CG-10", 215, 44, 15.3), (0, 0, "CG-12", 255, 38, 16.0), (10, 0, "CG-02", 290, 66,  9.8),
    (11, 0, "CG-05", 330, 30, 18.2), (1, 1, "CG-03", 372, 58, 11.1), (3, 0, "CG-07", 415, 46, 13.9),
    (8, 0, "CG-09", 455, 52, 10.7), (4, 0, "CG-04", 500, 34, 17.0), (6, 0, "CG-06", 540, 60,  9.1),
]


def demo_plates() -> set[str]:
    return {v["placa"] for c in DEMO_CLIENTS for v in c["veiculos"]}


def seed_clients() -> int:
    """Cadastra os clientes fictícios em client_store (idempotente). Devolve quantos foram criados agora."""
    created = 0
    for c in DEMO_CLIENTS:
        if client_store.account_exists(c["cpf"]):
            continue
        account = client_store.create_account(c["nome"], c["cpf"], c["email"], DEMO_PASSWORD)
        account["demo"] = True  # marca como conta de demonstração (usado pelo /demo/clientes)
        for v in c["veiculos"]:
            client_store.add_vehicle(c["cpf"], v["modelo"], v["placa"])
        created += 1
    return created


def history_items(now: datetime | None = None) -> list[dict]:
    """Sessões concluídas iniciais, com horários relativos a AGORA (terminaram há alguns minutos/horas)."""
    now = now or datetime.now()
    items = []
    for client_idx, vehicle_idx, charger_id, ended_ago_min, duration_min, avg_kw in _HISTORY:
        placa = DEMO_CLIENTS[client_idx]["veiculos"][vehicle_idx]["placa"]
        started_at = now - timedelta(minutes=ended_ago_min + duration_min)
        items.append({
            "charger_id": charger_id,
            "vehicle_id": placa,
            "started_at": started_at.isoformat(),
            "duration_min": float(duration_min),
            "energy_kwh": round(avg_kw * duration_min / 60, 3),
        })
    return items


# ----------------------------------------------------------------------
# Dias anteriores (histórico fictício) — alimenta o gráfico de faturamento
# semana/mês e o re-treino da IA de previsão/tarifa dinâmica.
# ----------------------------------------------------------------------

PAST_DAYS = 60


def _demand_shape_kw(hour: float, is_weekend: bool) -> float:
    """Demanda típica do estacionamento (kW) numa hora do dia — a MESMA forma que a IA
    aprende (backend/ai/generate_history.py): dois picos em dia útil (~8h e ~19h) e um
    platô mais baixo à tarde no fim de semana."""
    def hill(center, width, height):
        return height * math.exp(-((hour - center) ** 2) / (2 * width ** 2))
    if is_weekend:
        return hill(14, 4.0, 28) + 4
    return hill(8, 2.5, 35) + hill(19, 3.0, 52) + 6


def _poisson(rng: random.Random, lam: float) -> int:
    """Nº de chegadas numa hora (Knuth) — varia de dia para dia, como no mundo real."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def past_days_items(now: datetime | None = None, days: int = PAST_DAYS) -> list[dict]:
    """
    Sessões fictícias dos `days` dias ANTERIORES a hoje (formato de history_items()).

    Para cada hora, o nº de chegadas é sorteado para que a demanda média bata com a
    curva típica (demanda = chegadas x duração média x potência média). Cada dia usa
    uma semente própria (a data), então os mesmos dias geram as mesmas sessões em toda
    execução — importar de novo não duplica. Um carregador nunca recebe duas sessões
    ao mesmo tempo, e nenhuma sessão termina no futuro.
    """
    now = now or datetime.now()
    avg_kw_mean, avg_min_mean = 14.0, 45.0
    plate_rng = random.Random("demo-plates")
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    plates = [f"{''.join(plate_rng.choices(letters, k=3))}{plate_rng.randint(0, 9)}{plate_rng.choice(letters)}{plate_rng.randint(10, 99)}"
              for _ in range(90)]
    chargers = [f"CG-{i:02d}" for i in range(1, 13)]
    busy_until: dict[str, datetime] = {}
    items: list[dict] = []

    for offset in range(days, 0, -1):
        day = (now - timedelta(days=offset)).date()
        rng = random.Random(day.isoformat())
        midnight = datetime.combine(day, datetime.min.time())
        is_weekend = day.weekday() >= 5
        for hour in range(24):
            lam = _demand_shape_kw(hour + 0.5, is_weekend) / (avg_kw_mean * avg_min_mean / 60)
            lam *= rng.uniform(0.85, 1.15)             # dia bom / dia fraco
            arrivals = sorted(rng.randint(0, 59) for _ in range(_poisson(rng, lam)))
            for minute in arrivals:
                started = midnight + timedelta(hours=hour, minutes=minute)
                duration = round(min(120.0, max(15.0, rng.gauss(avg_min_mean, 18))))
                ended = started + timedelta(minutes=duration)
                if ended > now - timedelta(minutes=5):
                    continue
                free = [c for c in chargers if busy_until.get(c, datetime.min) <= started]
                if not free:
                    continue                            # vagas lotadas: o carro foi embora
                charger = rng.choice(free)
                busy_until[charger] = ended
                avg_kw = min(22.0, max(5.0, rng.gauss(avg_kw_mean, 4.0)))
                items.append({
                    "charger_id": charger,
                    "vehicle_id": rng.choice(plates),
                    "started_at": started.isoformat(),
                    "duration_min": float(duration),
                    "energy_kwh": round(avg_kw * duration / 60, 3),
                })
    return items
