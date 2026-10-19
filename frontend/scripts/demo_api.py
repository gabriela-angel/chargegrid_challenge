"""
ChargeGrid Intelligence — Cliente HTTP compartilhado pelos scripts de demo
---------------------------------------------------------------------------
Usado por seed_demo.py e demo_scenario.py (ficam na mesma pasta; rodando
`python scripts/seed_demo.py`, esta pasta já entra no sys.path). Fala com o
FRONTEND (porta 8001), nas rotas /demo/* do main.py, que iniciam/encerram a
recarga de um CLIENTE FICTÍCIO CADASTRADO pela mesma camada do app do cliente.
Só biblioteca padrão — não precisa instalar nada.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:8001"
CHARGER_IDS = [f"CG-{i:02d}" for i in range(1, 13)]

# Metas variadas (o que o cliente escolheria em "Meta da recarga") — assim as
# telas de "carregando" dos clientes não ficam todas iguais.
GOALS = [("tempo", 30), ("energia", 12), ("tempo", 45), ("energia", 18),
         ("tempo", 60), ("energia", 25), ("tempo", 40), ("energia", 15)]


def _call(host: str, method: str, path: str, data: dict | None = None):
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    if os.environ.get("CHARGEGRID_API_KEY"):       # mesma chave do servidor (ver backend/security.py)
        headers["X-API-Key"] = os.environ["CHARGEGRID_API_KEY"]
    req = urllib.request.Request(f"{host}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            detail = json.loads(raw).get("detail", raw)
        except ValueError:
            detail = raw
        return exc.code, detail
    except urllib.error.URLError as exc:
        print(f"\n  Não consegui conectar em {host} ({exc.reason}).")
        print("  Os dois servidores estão rodando? (./run.sh na raiz do projeto)")
        raise SystemExit(1)


def list_clients(host: str) -> list[dict]:
    status, data = _call(host, "GET", "/demo/clientes")
    if status == 404:
        print("\n  Modo demonstração desligado no servidor (DEMO_MODE=0) — nada a fazer.")
        raise SystemExit(1)
    if status != 200:
        print(f"\n  Erro {status}: {data}")
        print("  Exporte CHARGEGRID_API_KEY com o valor impresso pelo run.sh e tente de novo.")
        raise SystemExit(1)
    return data


def start_charge(host: str, client: dict, charger_id: str, goal: tuple[str, float]) -> bool:
    modo, valor = goal
    veiculo = client["veiculos"][0]
    status, data = _call(host, "POST", "/demo/cliente/iniciar", {
        "cpf": client["cpf"], "charger_id": charger_id, "placa": veiculo["placa"], "modo": modo, "valor": valor,
    })
    meta = f"{valor:g} min" if modo == "tempo" else f"{valor:g} kWh"
    if status == 200:
        print(f"  [+] {charger_id} <- {client['nome']} ({veiculo['modelo']}, {veiculo['placa']}) — meta {meta}")
        return True
    print(f"  [!] {charger_id} <- {client['nome']}: FALHOU ({status}) {data}")
    return False


def end_charge(host: str, client: dict) -> bool:
    status, data = _call(host, "POST", "/demo/cliente/encerrar", {"cpf": client["cpf"]})
    if status == 200:
        print(f"  [-] {client['nome']} encerrou a recarga")
        return True
    print(f"  [!] {client['nome']}: FALHOU ao encerrar ({status}) {data}")
    return False


def free_clients(host: str) -> list[dict]:
    """Clientes fictícios que NÃO estão carregando agora."""
    return [c for c in list_clients(host) if not c["carregando_em"]]
