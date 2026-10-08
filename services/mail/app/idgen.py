import secrets


def new_id(prefix: str) -> str:
    """Random id — mirrors chat's idgen (R68: abs(hash(...)) ids were
    deterministic per process and a collision/traceability sharp edge)."""
    return f"{prefix}_{secrets.token_hex(8)}"
