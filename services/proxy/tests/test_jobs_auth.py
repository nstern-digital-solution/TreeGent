"""R72b regression: authenticated job submission must not 500.

R69 added an actor-existence re-check to auth_agent() referencing db.actors
BEFORE any `actors` handle existed in the proxy's db.py — from the moment
central updated to R69, EVERY authenticated POST /v1/jobs died with
AttributeError (plain-text 500; clients saw JSONDecodeError) and no agent
could generate anything. This suite submits through a real uvicorn
subprocess — the exact prod shape — and fails if auth crashes again.

Requires TG_TEST_MONGO_URL pointing at a DISPOSABLE Mongo (the test wipes
treegent collections in it).
"""
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TG_TEST_MONGO_URL"),
    reason="TG_TEST_MONGO_URL not set — point it at a disposable Mongo")

PORT = 18097
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server():
    # wipe + seed via sync pymongo (no event-loop entanglement)
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    for col in ("agent_keys", "actors", "task_classes", "jobs",
                "usage_events"):
        db[col].delete_many({})
    db.agent_keys.insert_one({"_id": "sk-test", "agent_id": "agt_t"})
    db.actors.insert_one({"_id": "agt_t", "username": "t"})
    db.task_classes.insert_one({"_id": "agent"})
    mc.close()

    env = dict(os.environ,
               TG_MONGO_URL=os.environ["TG_TEST_MONGO_URL"],
               TG_SERVICE_TOKEN="test-token")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "services.proxy.app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level",
         "warning"],
        cwd=os.path.join(os.path.dirname(__file__), "..", "..", ".."),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("proxy test server did not come up")
        yield
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=10)


def test_submit_does_not_500(server):
    """The R69 regression: auth passes and the job queues (201)."""
    r = httpx.post(f"{BASE}/v1/jobs", headers={"X-Agent-Key": "sk-test"},
                   json={"class_name": "agent", "reason": "test",
                         "messages": [{"role": "user", "content": "hi"}],
                         "tools": [], "max_tokens": 8}, timeout=15)
    assert r.status_code == 201, f"{r.status_code}: {r.text[:200]}"
    assert r.json()["status"] == "queued"
    # R32: exactly one in flight — the second submit 409s, never 500s
    r2 = httpx.post(f"{BASE}/v1/jobs", headers={"X-Agent-Key": "sk-test"},
                    json={"class_name": "agent", "reason": "test",
                          "messages": [{"role": "user", "content": "hi"}],
                          "tools": [], "max_tokens": 8}, timeout=15)
    assert r2.status_code == 409, f"{r2.status_code}: {r2.text[:200]}"


def test_deleted_actor_key_rejected(server):
    """R69 intent preserved: a deleted actor's key no longer authorizes."""
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    mc["treegent"]["jobs"].delete_many({})
    mc["treegent"]["actors"].delete_one({"_id": "agt_t"})
    mc.close()
    r = httpx.post(f"{BASE}/v1/jobs", headers={"X-Agent-Key": "sk-test"},
                   json={"class_name": "agent", "reason": "test",
                         "messages": [{"role": "user", "content": "hi"}],
                         "tools": [], "max_tokens": 8}, timeout=15)
    assert r.status_code == 401, f"{r.status_code}: {r.text[:200]}"
