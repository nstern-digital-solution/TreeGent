"""Issue #29: /health on an agent host must not ping Mongo.

Agent hosts have no mongod by design (R56) — the unconditional
`client.admin.command('ping')` raised ServerSelectionTimeoutError on
every call: the runtime's health endpoint 500'd forever on a box whose
agents were running perfectly. Fix: tier branch on settings.host_key
(same predicate as _host_health_loop and _hosted()).

The health() coroutine is awaited directly — no TestClient, no app
startup, no background supervisor tasks, no server. The BoomClient
stands in for the unreachable Mongo of an agent box: ANY touch raises.
"""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import __main__ as runtime_main   # noqa: E402
from app.config import settings            # noqa: E402


class BoomClient:
    class admin:
        @staticmethod
        async def command(*a, **k):
            raise RuntimeError("Mongo touched on a hosted runtime!")


def test_health_hosted_ok_without_mongo():
    real_key, real = settings.host_key, runtime_main.client
    settings.host_key = "test-host-key"          # hosted tier
    runtime_main.client = BoomClient()           # no mongod, ever
    try:
        body = asyncio.run(runtime_main.health())
    finally:
        settings.host_key, runtime_main.client = real_key, real
    assert body == {"ok": True, "service": "runtime", "tier": "hosted"}, body
    print("PASS #29: hosted /health returns 200-body without Mongo")


def test_health_central_still_pings_mongo():
    real_key, real = settings.host_key, runtime_main.client
    settings.host_key = ""                        # central tier
    runtime_main.client = BoomClient()
    try:
        # the DB ping IS awaited: the Boom explodes — exactly the pre-fix
        # hosted behavior, now only on the tier where Mongo exists
        try:
            asyncio.run(runtime_main.health())
            raised = False
        except RuntimeError:
            raised = True
    finally:
        settings.host_key, runtime_main.client = real_key, real
    assert raised, "central /health must keep the Mongo check (its value)"
    print("PASS #29: central /health still awaits the Mongo ping")
