"""
ChargeGrid Intelligence — Contas de empresa / administrador (demo)
-------------------------------------------------------------------
Login separado do Dashboard de Gestão, no mesmo padrão "sem banco de
dados" do `client_store.py` (em memória; senha guardada só como hash
scrypt, ver security.py). Existe só para o Dashboard passar a exigir login
(ele era público antes) e para a tela de Configurações ter uma conta
de verdade para editar (nome do responsável / e-mail / senha).

Contrato: account_exists/check_login/start_session/end_session/
get_company_from_token/update_company — main.py só chama essas
funções, nunca mexe no dict interno diretamente.
"""

import secrets

from security import hash_password, verify_password

# e-mail normalizado (minúsculo) -> conta
companies: dict[str, dict] = {}

# token de sessão (cookie) -> e-mail normalizado
sessions: dict[str, str] = {}

SESSION_COOKIE = "cg_company_session"


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def account_exists(email: str) -> bool:
    return _normalize_email(email) in companies


def create_company(empresa: str, responsavel: str, email: str, senha: str) -> dict:
    email_norm = _normalize_email(email)
    account = {
        "empresa": empresa.strip(),
        "responsavel": responsavel.strip(),
        "email": email_norm,
        "senha_hash": hash_password(senha),  # nunca guarda a senha em texto
    }
    companies[email_norm] = account
    return account


def check_login(email: str, senha: str) -> dict | None:
    account = companies.get(_normalize_email(email))
    if account and verify_password(senha, account["senha_hash"]):
        return account
    return None


def start_session(email: str) -> str:
    token = secrets.token_urlsafe(24)
    sessions[token] = _normalize_email(email)
    return token


def end_session(token: str | None) -> None:
    if token:
        sessions.pop(token, None)


def get_company_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    email = sessions.get(token)
    return companies.get(email) if email else None


def update_company(email: str, empresa: str | None = None, responsavel: str | None = None,
                    novo_email: str | None = None, senha: str | None = None) -> dict | None:
    """Atualiza campos da conta (só os informados). Se novo_email vier
    preenchido e for diferente, migra a chave no dict e as sessões
    ativas continuam válidas (token -> email é atualizado junto)."""
    email_norm = _normalize_email(email)
    account = companies.get(email_norm)
    if not account:
        return None
    if empresa is not None:
        account["empresa"] = empresa.strip()
    if responsavel is not None:
        account["responsavel"] = responsavel.strip()
    if senha:
        account["senha_hash"] = hash_password(senha)
    novo_email_norm = _normalize_email(novo_email) if novo_email else None
    if novo_email_norm and novo_email_norm != email_norm:
        account["email"] = novo_email_norm
        companies[novo_email_norm] = account
        del companies[email_norm]
        for token, mapped_email in list(sessions.items()):
            if mapped_email == email_norm:
                sessions[token] = novo_email_norm
    return account


# ----------------------------------------------------------------------
# Conta demo pré-cadastrada — acesso imediato para apresentação/demo
# ----------------------------------------------------------------------
create_company(
    empresa="Shopping Paulista — Estacionamento",
    responsavel="Equipe ChargeGrid",
    email="contato@chargegrid.com",
    senha="demo123",
)
