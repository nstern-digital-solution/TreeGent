"""R78: model catalog context_window set/get + job-doc stamping.

Covers per the R78 spec:
- admin API accepts/returns context_window on a model (PUT policy + GET
  catalog); non-positive values are rejected
- a DONE job doc carries context_window for the served model (and the
  agent-facing GET /v1/jobs/{id} poll returns it — the runtime reads it
  from there)
- a catalog entry WITHOUT a window -> TG_DEFAULT_CONTEXT_WINDOW / 32768
  default stamped (never a None, never a failed job)

Runs through a real uvicorn subprocess (prod shape) with a stub
OpenAI-compatible provider (the same stub-provider shape as
test_embeddings.py). Requires TG_TEST_MONGO_URL pointing at a DISPOSABLE
Mongo (the test wipes treegent collections in it).
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

PORT = 18131
BASE = f"http://127.0.0.1:{PORT}"
PROV_PORT = 18132
HDR = {"X-Service-Token": "test-token"}
AGENT = {"X-Agent-Key": "sk-test"}


class StubProvider(BaseHTTPRequestHandler):
    """OpenAI-compatible chat-completions stub (no /models — catalog rows
    are seeded by the test, like the embeddings suite)."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        out = b'{"choices": [{"message": {"content": "stubbed."}}],' \
              b' "usage": {"prompt_tokens": 7, "completion_tokens": 3,' \
              b' "total_tokens": 10}}'
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
                "provider_health", "history"):
        db[col].delete_many({})
    db.agent_keys.insert_one({"_id": "sk-test", "agent_id": "agt_t"})
    db.actors.insert_one({"_id": "agt_t", "username": "t"})
    db.task_classes.insert_one({"_id": "agent"})
    db.providers.insert_one({
        "_id": "stub", "kind": "openai-compat",
        "base_url": f"http://127.0.0.1:{PROV_PORT}",
        "key_env": "TG_R78_TEST_KEY", "enabled": True})
    mc.close()

    env = dict(os.environ,
               TG_MONGO_URL=os.environ["TG_TEST_MONGO_URL"],
               TG_SERVICE_TOKEN="test-token",
               TG_R78_TEST_KEY="stub-key")
    env.pop("TG_DEFAULT_CONTEXT_WINDOW", None)   # default path under test
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


def _seed_model(model_id, **extra):
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    doc = {"_id": model_id, "provider": "stub", "listed": True,
           "designations": ["agent"], "rank": 5, "modalities": [],
           "excluded": False}
    doc.update(extra)
    mc["treegent"]["model_catalog"].delete_one({"_id": model_id})
    mc["treegent"]["model_catalog"].insert_one(doc)
    mc.close()


def _solo_model(model_id, **extra):
    """Exactly one eligible model — dispatch picks by rank and equal
    ranks would make the served model (and its stamp) nondeterministic."""
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    mc["treegent"]["model_catalog"].delete_many({})
    mc.close()
    _seed_model(model_id, **extra)


def _submit_and_wait(model):
    r = httpx.post(f"{BASE}/v1/jobs", headers=AGENT,
                   json={"class_name": "agent", "reason": "admin",
                         "messages": [{"role": "user", "content": "hi"}],
                         "tools": [], "max_tokens": 16}, timeout=15)
    assert r.status_code == 201, f"{r.status_code}: {r.text[:200]}"
    jid = r.json()["job_id"]
    for _ in range(120):
        g = httpx.get(f"{BASE}/v1/jobs/{jid}", headers=AGENT, timeout=15)
        assert g.status_code == 200, g.text[:200]
        body = g.json()
        if body["status"] in ("done", "failed"):
            return jid, body
        time.sleep(0.25)
    raise AssertionError(f"job for {model} did not reach a terminal state")


def test_catalog_context_window_set_get(server):
    """Admin API set/get: PUT policy accepts context_window, GET catalog
    returns it; non-positive values are rejected."""
    _seed_model("stub/chat-model")
    r = httpx.put(f"{BASE}/admin/models/stub/chat-model/policy", headers=HDR,
                  json={"context_window": 128000}, timeout=15)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    assert r.json().get("context_window") == 128000, r.json()
    r = httpx.get(f"{BASE}/admin/catalog", headers=HDR, timeout=15)
    rows = {m["id"]: m for m in r.json()}
    assert rows["stub/chat-model"]["context_window"] == 128000, rows
    # invalid windows never land in the catalog
    for bad in (0, -5):
        r = httpx.put(f"{BASE}/admin/models/stub/chat-model/policy",
                      headers=HDR, json={"context_window": bad}, timeout=15)
        assert r.status_code == 400, f"{bad} accepted: {r.text[:200]}"
    r = httpx.get(f"{BASE}/admin/catalog", headers=HDR, timeout=15)
    rows = {m["id"]: m for m in r.json()}
    assert rows["stub/chat-model"]["context_window"] == 128000, rows
    # unset models come back as null, never crash the endpoint
    _seed_model("stub/no-window-model")
    r = httpx.get(f"{BASE}/admin/catalog", headers=HDR, timeout=15)
    rows = {m["id"]: m for m in r.json()}
    assert rows["stub/no-window-model"]["context_window"] is None, rows


def test_done_job_doc_carries_context_window(server):
    """A done job doc stamps the served model's window — visible on the
    agent-facing poll (the runtime reads it there) and in Mongo."""
    _solo_model("stub/chat-model", context_window=128000)
    jid, body = _submit_and_wait("stub/chat-model")
    assert body["status"] == "done", body
    assert body.get("context_window") == 128000, body   # the poll carries it
    assert body["result"]["usage"]["prompt_tokens"] == 7, body
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    doc = mc["treegent"]["jobs"].find_one({"_id": jid})
    mc.close()
    assert doc["context_window"] == 128000, doc
    assert doc["model"] == "stub/chat-model", doc


def test_missing_window_default_stamped(server):
    """Missing-window fallback: the stamp is the conservative default
    (32768 without TG_DEFAULT_CONTEXT_WINDOW) — never None, never a
    failed job."""
    _solo_model("stub/no-window-model")
    jid, body = _submit_and_wait("stub/no-window-model")
    assert body["status"] == "done", body
    assert body.get("context_window") == 32768, body
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    doc = mc["treegent"]["jobs"].find_one({"_id": jid})
    mc.close()
    assert doc["context_window"] == 32768, doc


def test_default_context_window_env(monkeypatch):
    """TG_DEFAULT_CONTEXT_WINDOW override + garbage tolerance (in-process
    unit check of the fallback)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    ".."))
    os.environ.setdefault("TG_DEV", "1")
    from app import dispatcher

    monkeypatch.setenv("TG_DEFAULT_CONTEXT_WINDOW", "99999")
    assert dispatcher._default_context_window() == 99999
    monkeypatch.setenv("TG_DEFAULT_CONTEXT_WINDOW", "garbage")
    assert dispatcher._default_context_window() == 32768
    monkeypatch.setenv("TG_DEFAULT_CONTEXT_WINDOW", "0")
    assert dispatcher._default_context_window() == 32768
    monkeypatch.delenv("TG_DEFAULT_CONTEXT_WINDOW", raising=False)
    assert dispatcher._default_context_window() == 32768
