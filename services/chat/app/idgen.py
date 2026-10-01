import secrets
from datetime import datetime, timezone

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def oid() -> str:
    """Sortable-ish object id: time-ordered prefix + random tail."""
    t = int(datetime.now(timezone.utc).timestamp())
    tail = "".join(secrets.choice(ALPHABET) for _ in range(10))
    return f"{t:x}{tail}"


def now() -> datetime:
    return datetime.now(timezone.utc)
