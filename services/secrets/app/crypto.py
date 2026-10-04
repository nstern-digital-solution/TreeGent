"""Value encryption: one master key (Fernet), stored ONLY in a local file
outside Mongo and the repo (instance data — same rule as provider API
keys). Values are encrypted at rest; metadata never is."""
import base64
import os

from cryptography.fernet import Fernet

from .config import settings

_fernet: Fernet | None = None


def _load_or_create_key() -> bytes:
    import os as _os
    env_key = _os.environ.get("TG_SECRETS_MASTER_KEY", "").strip()
    if env_key:
        return env_key.encode()
    path = settings.key_file
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read().strip()
            if key:
                return key
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, key)
    os.close(fd)
    return key


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(value: str) -> str:
    return fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return fernet().decrypt(token.encode()).decode()
