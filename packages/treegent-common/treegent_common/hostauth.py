"""R56 host-tier authentication (shared by chat + runtime).

Agent hosts get NO database credentials (R56): the hosted runtime is a
pure HTTPS client of the central services. It authenticates with a
per-host secret issued at provision time:

    X-Host-Id:  host_<id>           (routing/lookup)
    X-Host-Key: host_<random>       (proof; hash lives in agent_hosts.host_key_hash)

The secret is generated on the central box, written into the agent host's
runtime.env, shown once, and never stored in plaintext anywhere. Chat
validates it against the stored hash with a constant-time compare.

This replaces the pre-R56 design where the runtime held TG_MONGO_URL and
read Mongo directly — which handed a full read/write credential to every
machine an agent executes code on.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

HEADER_HOST_ID = "X-Host-Id"
HEADER_HOST_KEY = "X-Host-Key"


def new_host_key() -> str:
    """Generate a fresh host secret (call once per provision)."""
    return "host_" + secrets.token_hex(24)


def hash_host_key(host_key: str) -> str:
    """Stable hash for storage. Not salted per-instance (the key itself is
    192-bit random), but sha256 keeps the plaintext out of Mongo dumps."""
    return hashlib.sha256(host_key.encode()).hexdigest()


def verify_host_key(host_key: str, stored_hash: str | None) -> bool:
    """Constant-time check; empty/missing hash never validates."""
    if not stored_hash or not host_key:
        return False
    return hmac.compare_digest(hash_host_key(host_key), stored_hash)
