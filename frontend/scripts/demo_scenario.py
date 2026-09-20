"""
ChargeGrid Intelligence — Cenário de demonstração (chegada → pico → alívio)
-------------------------------------------------------------------------------
FERRAMENTA DE BASTIDOR, não é uma feature do produto — mesma categoria do
`seed_demo.py`, mas pensada pra RODAR AO VIVO durante a apresentação/pitch,
em vez de só preencher o grid instantaneamente antes de gravar.

Dirige o sistema DE VERDADE pelas 3 fases, via HTTP — o dashboard atualiza
sozinho por WebSocket, a energia acumula (tick do backend real) e a
redistribuição de potência acontece de fato a cada carro que entra/sai.

Quem carrega são CLIENTES FICTÍCIOS JÁ CADASTRADOS (ver demo_seed.py): cada
recarga passa pela mesma camada do app do cliente (meta escolhida, "minha
recarga", recibo, histórico na conta) — só o pagamento é pulado. Então dá pra,
no meio da demo, logar como um deles em /cliente/login (CPF + senha; rode
`python scripts/seed_demo.py --listar`) e ver a recarga em andamento no celular.

Uso (servidores rodando — ex: `./run.sh` na raiz do projeto):

    python scripts/demo_scenario.py
    python scripts/demo_scenario.py --speed 2.0     # 2x mais rápido
    python scripts/demo_scenario.py --host http://localhost:8001
"""

import argparse
import random
import time

import demo_api


def fase(titulo: str) -> None:
    print(f"\n=== {titulo} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=demo_api.DEFAULT_HOST, help="Base URL do FRONTEND (não do backend)")
    parser.add_argument("--speed", type=float, default=1.0, help="Multiplicador de velocidade (2.0 = roda 2x mais rápido)")
    args = parser.parse_args()

    def espera(segundos_base: float) -> None:
        time.sleep(segundos_base / args.speed)

    clients = demo_api.free_clients(args.host)
    if len(clients) < 12:
        print(f"[demo_scenario] Só {len(clients)} cliente(s) livre(s) — o cenário completo usa 12 (um por vaga).")
        print("  Encerre as recargas em andamento (ou reinicie os servidores) e rode de novo.")
        raise SystemExit(1)

    ids = demo_api.CHARGER_IDS
    goals = demo_api.GOALS
    print(f"[demo_scenario] Rodando contra {args.host} (velocidade {args.speed}x)")

    fase("FASE 1 — Chegada gradual (6 clientes, ~1 a cada 2s)")
    for i in range(6):
        demo_api.start_charge(args.host, clients[i], ids[i], goals[i % len(goals)])
        espera(2.0)

    fase("FASE 2 — Congestionamento máximo (os outros 6 clientes, rápido)")
    for i in range(6, 12):
        demo_api.start_charge(args.host, clients[i], ids[i], goals[i % len(goals)])
        espera(0.5)
    print("  Site 100% ocupado (12/12) — observem a redistribuição de potência no dashboard.")
    espera(4.0)

    fase("FASE 3 — Alívio final (clientes encerrando em ordem aleatória)")
    ordem_saida = list(clients[:12])
    random.shuffle(ordem_saida)
    for client in ordem_saida:
        demo_api.end_charge(args.host, client)
        espera(1.5)

    print("\n[demo_scenario] Concluído — site livre de novo. Histórico em /dashboard/historico e, por cliente, em /cliente/historico.")


if __name__ == "__main__":
    main()
