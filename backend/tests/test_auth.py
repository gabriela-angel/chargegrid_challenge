"""
Testes da chave de API do backend e do limite da lista de eventos.
Rodar de dentro de backend/:  python -m pytest -q
"""
import pytest
from fastapi.testclient import TestClient

from core.demand_controller import MAX_EVENTS, DemandController
from main import app

client = TestClient(app)   # sem "with": não dispara o lifespan (loop de ticks)


def test_sem_chave_configurada_a_api_fica_aberta(monkeypatch):
    monkeypatch.delenv("CHARGEGRID_API_KEY", raising=False)
    assert client.get("/api/chargers").status_code == 200


def test_com_chave_configurada_exige_o_cabecalho(monkeypatch):
    monkeypatch.setenv("CHARGEGRID_API_KEY", "segredo-de-teste")
    assert client.get("/api/chargers").status_code == 401
    assert client.get("/api/chargers", headers={"X-API-Key": "errada"}).status_code == 401
    assert client.get("/api/chargers", headers={"X-API-Key": "segredo-de-teste"}).status_code == 200


@pytest.mark.parametrize("method,path", [
    ("post", "/api/chargers/CG-01/connect"),
    ("post", "/api/chargers/CG-01/disconnect"),
    ("put", "/api/tariff/config"),
    ("put", "/api/solar/config"),
    ("post", "/api/predictions/retrain"),
    ("get", "/api/hardware"),
])
def test_rotas_que_comandam_o_sistema_exigem_a_chave(monkeypatch, method, path):
    monkeypatch.setenv("CHARGEGRID_API_KEY", "segredo-de-teste")
    assert getattr(client, method)(path).status_code == 401


def test_lista_de_eventos_tem_limite():
    c = DemandController("Teste", 100.0)
    for i in range(MAX_EVENTS + 200):
        c._log_event("TESTE", "SYSTEM", f"evento {i}")
    assert len(c.events) == MAX_EVENTS
    assert c.events[-1]["message"] == f"evento {MAX_EVENTS + 199}"   # os mais recentes ficam
