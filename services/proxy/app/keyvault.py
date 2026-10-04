"""Encrypted provider-key storage: keys live encrypted in the providers
collection (Fernet under TG_SECRETS_MASTER_KEY or TG_PROXY_KEYFILE),
never in env files, never returned by any API. Write-only via admin."""
import os

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_cache: dict = {}


def _fer() -> Fernet | None:
    mk = os.environ.get("TG_SECRETS_MASTER_KEY", "")
    if not mk:
        path = os.environ.get("TG_PROXY_KEYFILE",
                              os.path.expanduser("~/.treegent/keys.key"))
        try:
            with open(path) as f:
                mk = f.read().strip()
        except FileNotFoundError:
            return None
    return Fernet(mk.encode() if isinstance(mk, str) else mk)


def encrypt_key(plain: str) -> str:
    f = _fer()
    if f is None:
        raise RuntimeError("no master key configured for provider keys")
    return f.encrypt(plain.encode()).decode()


def decrypt_key(enc: str) -> str | None:
    f = _fer()
    if f is None or not enc:
        return None
    try:
        return f.decrypt(enc.encode()).decode()
    except InvalidToken:
        return None


def generate_keyfile(path: str | None = None) -> str:
    path = path or os.path.expanduser("~/.treegent/keys.key")
    k = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(k.decode())
    os.chmod(path, 0o600)
    return path
