"""
ChargeGrid Intelligence — Chave de API do backend
---------------------------------------------------
O backend (porta 8000) comanda os carregadores, então não pode ficar aberto
para qualquer um que alcance a porta. Com a variável de ambiente
CHARGEGRID_API_KEY definida, toda rota exige o cabeçalho `X-API-Key` igual a ela:

    - o frontend envia a chave em todas as chamadas (frontend/backend/remote_controller.py);
    - o ESP32 / simulate_esp32.py enviam o mesmo cabeçalho;
    - o ./run.sh gera uma chave aleatória a cada execução, se você não definir uma.

Sem a variável (desenvolvimento solto) o backend segue aberto e avisa no log —
decisão consciente para não atrapalhar quem roda só um pedaço do projeto.
"""

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger("chargegrid.auth")


def configured_key() -> str:
    """Chave esperada ('' = sem proteção). Lida a cada chamada (permite testes e rotação)."""
    return os.environ.get("CHARGEGRID_API_KEY", "").strip()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Dependência do FastAPI: 401 se a chave estiver configurada e o cabeçalho não bater."""
    expected = configured_key()
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="X-API-Key ausente ou inválida.")


def warn_if_open() -> None:
    """Aviso na partida do servidor quando não há chave configurada."""
    if not configured_key():
        logger.warning("CHARGEGRID_API_KEY não definida — a API está ABERTA (ok só em desenvolvimento local).")
