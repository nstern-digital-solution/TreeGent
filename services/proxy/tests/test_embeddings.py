"""R76: POST /v1/embeddings through the real uvicorn subprocess (prod shape).

Covers per the R76 spec:
- no embedding model configured -> clean 503 (runtime degrades gracefully)
- with a stub provider row -> 200 + OpenAI response shape + usage row
- bad key -> 401 (same X-Agent-Key auth as /v1/jobs)

Requires TG_TEST_MONGO_URL pointing at a DISPOSABLE Mongo (the test wipes
treegent collections in it).
"""
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TG_TEST_MONGO_URL"),
    reason="TG_TEST_MONGO_URL not set — point it at a disposable Mongo")

PORT = 18098
BASE = f"http://127.0.0.1:{PORT}"
PROV_PORT = 18099
EMB_VECTOR = [0.25, -0.5, 0.75]


class StubProvider(BaseHTTPRequestHandler):
    """OpenAI-compatible embeddings stub (deliberately NO /models — the
    catalog rows are seeded by the admin, exactly the spec's 'stub
    provider row' shape)."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        import json
        req = json.loads(body)
        inputs = req["input"]
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [{"embedding": EMB_VECTOR, "index": i} for i in range(len(inputs))]
        out = json.dumps({
            "object": "list", "model": req.get("model", "emb-model"),
            "data": data,
            "usage": {"prompt_tokens": 7 * len(inputs),
                      "total_tokens": 7 * len(inputs)},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self.send_response(404)   # no /models — seed the catalog row instead
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture(scope="module")
def stub_provider():
    srv = HTTPServer(("127.0.0.1", PROV_PORT), StubProvider)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield
    srv.shutdown()


@pytest.fixture(scope="module")
def server(stub_provider):
    # wipe + seed via sync pymongo (no event-loop entanglement)
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    for col in ("agent_keys", "actors", "task_classes", "jobs",
                "usage_events", "providers", "model_catalog",
                "provider_health"):
        db[col].delete_many({})
    db.agent_keys.insert_one({"_id": "sk-test", "agent_id": "agt_t"})
    db.actors.insert_one({"_id": "agt_t", "username": "t"})
    db.task_classes.insert_one({"_id": "agent"})
    mc.close()

    env = dict(os.environ,
               TG_MONGO_URL=os.environ["TG_TEST_MONGO_URL"],
               TG_SERVICE_TOKEN="test-token",
               TG_EMB_TEST_KEY="stub-key")
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


def test_bad_key_401(server):
    """Same auth as /v1/jobs: a bad agent key never reaches a provider."""
    r = httpx.post(f"{BASE}/v1/embeddings",
                   headers={"X-Agent-Key": "sk-wrong"},
                   json={"input": "hello"}, timeout=15)
    assert r.status_code == 401, f"{r.status_code}: {r.text[:200]}"
    r = httpx.post(f"{BASE}/v1/embeddings", json={"input": "hello"}, timeout=15)
    assert r.status_code == 401, f"{r.status_code}: {r.text[:200]}"


def test_no_embedding_model_503(server):
    """Fresh/unconfigured deploy: the 'embedding' class is seeded but no
    model carries it -> clean 503 'no embedding model', never a 500."""
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    # the class seed ran at boot even though the test seeded only 'agent'
    assert db.task_classes.find_one({"_id": "embedding"}), \
        "embedding designation must be seeded on boot"
    db["model_catalog"].delete_many({})
    db["providers"].delete_many({})
    mc.close()
    r = httpx.post(f"{BASE}/v1/embeddings",
                   headers={"X-Agent-Key": "sk-test"},
                   json={"input": "hello"}, timeout=15)
    assert r.status_code == 503, f"{r.status_code}: {r.text[:200]}"
    assert "no embedding model" in r.text, r.text[:200]
    # a batch input hits the same clean 503
    r = httpx.post(f"{BASE}/v1/embeddings",
                   headers={"X-Agent-Key": "sk-test"},
                   json={"input": ["a", "b"]}, timeout=15)
    assert r.status_code == 503, f"{r.status_code}: {r.text[:200]}"


def test_embeddings_ok_openai_shape_and_usage(server):
    """Stub provider row: 200, OpenAI shape (data[i].embedding + index),
    and a metering usage row of class 'embedding'."""
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    db["providers"].insert_one({
        "_id": "stub", "kind": "openai-compat",
        "base_url": f"http://127.0.0.1:{PROV_PORT}",
        "key_env": "TG_EMB_TEST_KEY", "enabled": True})
    db["model_catalog"].insert_one({
        "_id": "stub/emb-model", "provider": "stub", "listed": True,
        "designations": ["embedding"], "rank": 5,
        "modalities": ["embedding"], "excluded": False})
    mc.close()

    r = httpx.post(f"{BASE}/v1/embeddings",
                   headers={"X-Agent-Key": "sk-test"},
                   json={"input": "hello"}, timeout=15)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body["data"][0]["embedding"] == EMB_VECTOR, body
    assert body["data"][0]["index"] == 0, body
    # batch input -> one row per input, indexed
    r = httpx.post(f"{BASE}/v1/embeddings",
                   headers={"X-Agent-Key": "sk-test"},
                   json={"input": ["hello", "world"], "designation": "embedding"},
                   timeout=15)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    rows = sorted(r.json()["data"], key=lambda d: d["index"])
    assert [d["index"] for d in rows] == [0, 1], rows
    assert all(d["embedding"] == EMB_VECTOR for d in rows), rows
    # metering: usage rows written with class 'embedding'
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    usage = list(mc["treegent"]["usage_events"].find({"class": "embedding"}))
    mc.close()
    assert usage, "metering row missing"
    assert usage[0]["tokens_in"] == 7, usage[0]   # single-input call
    u = usage[-1]
    assert u["agent_id"] == "agt_t", u
    assert u["model"] == "stub/emb-model", u
    assert u["tokens_in"] == 14, u        # per-call: 7 per input, 2 in batch
    assert u["status"] == "ok", u


def test_unknown_designation_400(server):
    r = httpx.post(f"{BASE}/v1/embeddings",
                   headers={"X-Agent-Key": "sk-test"},
                   json={"input": "hello", "designation": "nope"}, timeout=15)
    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"
