"""
ChargeGrid Intelligence — Segurança do frontend
-------------------------------------------------
Senhas com hash e a chave de API compartilhada com o backend. Só biblioteca
padrão (hashlib/hmac/secrets) — nada novo para instalar.

Senhas: scrypt (memory-hard) com salt aleatório por senha. O texto guardado é
    scrypt$<n>$<r>$<p>$<salt em hex>$<hash em hex>
então os parâmetros viajam junto e podem ser endurecidos no futuro sem quebrar
senhas antigas.

Chave de API (CHARGEGRID_API_KEY):
    - o backend (porta 8000) exige o cabeçalho X-API-Key quando a variável está definida;
    - o frontend envia essa chave em toda chamada ao backend (ver remote_controller.py);
    - as rotas /demo/* aceitam a mesma chave, para os scripts de bastidor.
"""

import hashlib
import hmac
import os
import secrets

_N, _R, _P = 2 ** 14, 8, 1          # custo do scrypt (~50 ms por senha)
_SALT_BYTES = 16


def hash_password(senha: str) -> str:
    """Gera o texto a guardar no lugar da senha."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(senha.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(senha: str, stored: str) -> bool:
    """True se `senha` corresponde ao hash guardado. Comparação em tempo constante."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(senha.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def api_key() -> str:
    """Chave de API configurada ('' = não configurada). Lida a cada chamada, para
    refletir o .env já carregado e permitir testes que mudam a variável."""
    return os.environ.get("CHARGEGRID_API_KEY", "").strip()


def key_matches(provided: str | None) -> bool:
    """True se `provided` é a chave configurada (comparação em tempo constante)."""
    expected = api_key()
    return bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)
