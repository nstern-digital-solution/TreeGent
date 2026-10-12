"""services/files test harness (first-ever suite, issue #51 F).

Shape mirrors services/mail + services/chat: a REAL uvicorn subprocess
(the prod shape) against a DISPOSABLE Mongo (TG_TEST_MONGO_URL), with
TG_DEV=1 so byte storage is the in-memory backend and no S3 host is
needed. Each module owns its server fixture + seeded state; the database
is dropped after every module so suites stay composable.
"""
import contextlib
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

MONGO_URL = os.environ.get("TG_TEST_MONGO_URL", "")
TOK = "test-token"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

COLLECTIONS = ("actors", "files", "folders", "permissions", "file_shares",
               "agent_keys")


def require_mongo():
    if not MONGO_URL:
        pytest.skip("TG_TEST_MONGO_URL not set — point it at a disposable Mongo")


def db():
    import pymongo
    return pymongo.MongoClient(MONGO_URL, directConnection=True)["treegent"]


def wipe():
    d = db()
    for c in COLLECTIONS:
        d[c].delete_many({})


def seed_org():
    """root human -> boss agent -> alice agent; bob and carol are peers."""
    from treegent_common.perms import RULE_SEEDS
    d = db()
    d.permissions.insert_many([dict(r) for r in RULE_SEEDS])
    d.actors.insert_many([
        {"_id": "hum_root", "username": "root", "display_name": "Root",
         "kind": "human",
         "org": {"parent_id": None, "ancestors": [], "depth": 0}},
        {"_id": "agt_boss", "username": "boss", "display_name": "Boss",
         "kind": "agent",
         "org": {"parent_id": "hum_root", "ancestors": ["hum_root"],
                 "depth": 1}},
        {"_id": "agt_alice", "username": "alice", "display_name": "Alice",
         "kind": "agent",
         "org": {"parent_id": "agt_boss",
                 "ancestors": ["hum_root", "agt_boss"], "depth": 2}},
        {"_id": "agt_bob", "username": "bob", "display_name": "Bob",
         "kind": "agent",
         "org": {"parent_id": "hum_root", "ancestors": ["hum_root"],
                 "depth": 1}},
        {"_id": "agt_carol", "username": "carol", "display_name": "Carol",
         "kind": "agent",
         "org": {"parent_id": "hum_root", "ancestors": ["hum_root"],
                 "depth": 1}},
    ])


def hdr(actor_id: str) -> dict:
    """Central tier: trusted same-host caller acting FOR that actor."""
    return {"X-Service-Token": TOK, "X-Actor-Id": actor_id}


@contextlib.contextmanager
def running_server(port: int):
    """Boot services.files.app.__main__:app (the prod entrypoint)."""
    env = dict(os.environ, TG_MONGO_URL=MONGO_URL, TG_SERVICE_TOKEN=TOK,
               TG_DEV="1")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "services.files.app.__main__:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            out = p.stdout.read().decode() if p.stdout else ""
            pytest.fail(f"files test server did not come up:\n{out}")
        yield base
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=10)


@pytest.fixture(scope="module", autouse=True)
def _fresh_db_per_module(request):
    yield
    if not MONGO_URL:
        return
    import pymongo
    client = pymongo.MongoClient(MONGO_URL, directConnection=True)
    client.drop_database("treegent")
    client.close()
