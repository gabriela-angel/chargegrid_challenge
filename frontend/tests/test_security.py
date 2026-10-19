"""
Testes de segurança do frontend: hash de senha, login e rotas protegidas.
Rodar de dentro de frontend/:  python -m pytest -q tests
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import client_store
import security
from main import app

client = TestClient(app)   # sem "with": não dispara o lifespan (não precisa do backend)


# ---------------- hash de senha ----------------
def test_hash_nao_guarda_a_senha_e_usa_salt():
    a, b = security.hash_password("demo123"), security.hash_password("demo123")
    assert "demo123" not in a
    assert a != b                      # salt aleatório: mesma senha, hashes diferentes
    assert a.startswith("scrypt$")


def test_verify_password():
    h = security.hash_password("demo123")
    assert security.verify_password("demo123", h) is True
    assert security.verify_password("outra", h) is False
    assert security.verify_password("demo123", "lixo") is False       # hash malformado não explode


def test_contas_guardam_so_o_hash():
    account = client_store.create_account("Teste", "529.982.247-25", "t@t.com", "minhasenha")
    assert "senha" not in account
    assert client_store.check_login("52998224725", "minhasenha") is not None
    assert client_store.check_login("52998224725", "errada") is None


# ---------------- chave de API ----------------
def test_key_matches(monkeypatch):
    monkeypatch.delenv("CHARGEGRID_API_KEY", raising=False)
    assert security.key_matches("qualquer") is False       # sem chave configurada, nada confere
    monkeypatch.setenv("CHARGEGRID_API_KEY", "abc")
    assert security.key_matches("abc") is True
    assert security.key_matches("abd") is False
    assert security.key_matches(None) is False


# ---------------- rotas protegidas ----------------
@pytest.mark.parametrize("method,path,data", [
    ("post", "/connect", {"charger_id": "CG-01", "vehicle_id": "X"}),
    ("post", "/disconnect", {"charger_id": "CG-01"}),
    ("get", "/sessions", None),
    ("post", "/chat", {"message": "oi"}),
    ("post", "/charger/CG-01/demo/payment", {"status": "pago"}),
    ("post", "/charger/CG-01/demo/flag", {"flag": "erro"}),
])
def test_rotas_do_operador_exigem_login(method, path, data):
    assert getattr(client, method)(path, **({"data": data} if data else {})).status_code == 401


def test_websocket_recusa_quem_nao_esta_logado():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_rotas_demo_exigem_chave_ou_login(monkeypatch):
    monkeypatch.setenv("CHARGEGRID_API_KEY", "segredo-de-teste")
    assert client.get("/demo/clientes").status_code == 401
    assert client.get("/demo/clientes", headers={"X-API-Key": "errada"}).status_code == 401
    assert client.get("/demo/clientes", headers={"X-API-Key": "segredo-de-teste"}).status_code == 200


def test_operador_logado_acessa_as_rotas():
    c = TestClient(app)
    login = c.post("/empresa/login", data={"email": "contato@chargegrid.com", "senha": "demo123"},
                   follow_redirects=False)
    assert login.status_code == 303
    assert c.get("/sessions").status_code == 200
    errada = TestClient(app).post("/empresa/login", data={"email": "contato@chargegrid.com", "senha": "x"})
    assert errada.status_code == 401 and "incorretos" in errada.text      # re-renderiza o login com erro


def test_websocket_aceita_operador_logado():
    c = TestClient(app)
    c.post("/empresa/login", data={"email": "contato@chargegrid.com", "senha": "demo123"}, follow_redirects=False)
    with c.websocket_connect("/ws") as ws:
        assert "chargers" in ws.receive_json()
