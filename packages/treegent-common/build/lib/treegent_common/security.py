"""R62 fail-closed credential checks (security finding 06).

The dev default token and empty/placeholder credentials must never
authorize a production deployment. Services refuse to boot with them
unless TG_DEV=1 explicitly marks a development machine.
"""
import os

# values that must never guard a production deployment
INSECURE_TOKENS = {"", "dev-service-token", "change-me-random"}


def dev_mode() -> bool:
    return os.environ.get("TG_DEV", "") == "1"


def assert_secure_service_token(token: str, service: str) -> None:
    """Raise at import/boot time when a service would run with a
    publicly known credential. TG_DEV=1 opts in explicitly (rehearsal,
    dev boxes); agent hosts set nothing and their central-tier is
    unreachable anyway (R45: no Mongo, loopback-only runtime)."""
    if token in INSECURE_TOKENS and not dev_mode():
        raise RuntimeError(
            f"{service}: TG_SERVICE_TOKEN is empty/dev-default — refusing to start. "
            "Set a real token (quickstart/install.sh generate one) or export "
            "TG_DEV=1 for an explicit development machine."
        )
