"""
ChargeGrid Intelligence — seed_demo.py
---------------------------------------
FERRAMENTA DE BASTIDOR, não é uma feature do produto. Ocupa várias vagas de
uma vez ANTES de gravar a demo, sem repetir o fluxo do app (login → carregar →
pagar) uma vez por carregador.

Agora quem carrega são CLIENTES FICTÍCIOS JÁ CADASTRADOS (demo_seed.py), não
placas soltas: cada recarga fica amarrada a uma conta (aparece em Clientes,
Histórico e na tela "carregando" do próprio cliente ao fazer login), com meta
de recarga escolhida. Só o pagamento é pulado.

Uso (servidores rodando — ex.: `./run.sh` na raiz do projeto):

    python scripts/seed_demo.py                  # ocupa 6 vagas (padrão)
    python scripts/seed_demo.py --count 12       # ocupa todas as 12
    python scripts/seed_demo.py --listar         # mostra os clientes e como logar
    python scripts/seed_demo.py --host http://localhost:8001
"""

import argparse

import demo_api


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=demo_api.DEFAULT_HOST, help="Base URL do FRONTEND (porta 8001)")
    parser.add_argument("--count", type=int, default=6, help="Quantas vagas ocupar (1-12, padrão 6)")
    parser.add_argument("--listar", action="store_true", help="Só lista os clientes fictícios e as credenciais de login")
    args = parser.parse_args()

    if args.listar:
        clients = demo_api.list_clients(args.host)
        print(f"\nClientes fictícios ({len(clients)}) — login em {args.host}/cliente/login com CPF + senha:\n")
        for c in clients:
            estado = f"  (carregando em {c['carregando_em']})" if c["carregando_em"] else ""
            print(f"  {c['cpf']}  senha: {c['senha']}  {c['nome']:<18} {', '.join(v['placa'] for v in c['veiculos'])}{estado}")
        return

    count = max(1, min(12, args.count))
    clients = demo_api.free_clients(args.host)
    if len(clients) < count:
        print(f"[seed_demo] Só {len(clients)} cliente(s) livre(s) — vou ocupar {len(clients)} vaga(s).")
        count = len(clients)

    print(f"[seed_demo] Ocupando {count} vaga(s) em {args.host} com clientes cadastrados ...")
    for i in range(count):
        demo_api.start_charge(args.host, clients[i], demo_api.CHARGER_IDS[i], demo_api.GOALS[i % len(demo_api.GOALS)])
    print("[seed_demo] Concluído.")


if __name__ == "__main__":
    main()
