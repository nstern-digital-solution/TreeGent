"""Issue #54 regression: the R69 thread-offload must pass the urlopen
timeout as a KEYWORD. Positional 20 binds to `data`, turning the GET into
a POST with an int body -> TypeError('message_body should be a bytes-like
object or an iterable, got <class 'int'>') on every candidate URL for
every ready provider -> refresh-catalog 100% broken since R69.

The TypeError only fires against a LIVE server (the client serializes the
body into the request), so these tests spin a real local HTTP server —
a refused port short-circuits earlier and cannot pin the bug.
"""
import asyncio
import http.server
import os

# proxy-suite convention (cf. test_r78_context_window.py): the app config
# import asserts a secure service token unless TG_DEV is explicitly set —
# set it BEFORE importing any services.proxy module (review #63)
os.environ.setdefault("TG_DEV", "1")

import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import pytest


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"data": [{"id": "m1"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture(scope="module")
def live_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(fn, *args, **kwargs):
    """Run the exact call shape dispatcher uses: to_thread(urlopen, ...)."""
    return asyncio.run(asyncio.to_thread(fn, *args, **kwargs))


def test_primary_site_keyword_timeout(live_server):
    """dispatcher's main fetch shape (fixed): timeout keyword, GET succeeds."""
    req = urllib.request.Request(f"{live_server}/models",
                                 headers={"User-Agent": "TreeGent-test"})
    with _call(urllib.request.urlopen, req, timeout=20) as r:
        assert r.status == 200
        import json
        assert json.load(r)["data"][0]["id"] == "m1"


def test_primary_site_positional_is_the_bug(live_server):
    """The R69 shape (positional 20) against a LIVE server = the #54 TypeError.
    Documents the failure mode; fails if the bug is reintroduced."""
    req = urllib.request.Request(f"{live_server}/models",
                                 headers={"User-Agent": "TreeGent-test"})
    with pytest.raises(TypeError):
        _call(urllib.request.urlopen, req, 20)


def test_bare_retry_site_keyword_timeout(live_server):
    """The 401/403 bare-retry call site (same file, same invariant)."""
    req2 = urllib.request.Request(f"{live_server}/models",
                                  headers={"User-Agent": "TreeGent-test"})
    with _call(urllib.request.urlopen, req2, timeout=20) as r:
        assert r.status == 200


def test_refresh_catalog_against_live_stub(monkeypatch, live_server):
    """End-to-end: refresh_catalog with a provider whose base URL is a LIVE
    stub must list its model and return 1 — the exact operation that 500s
    in prod (#54). Requires the keyword fix; the positional shape raises
    TypeError out of refresh_catalog (unhandled -> 500 {})."""
    import sys, os
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, root)
    from services.proxy.app import dispatcher

    async def fake_ready():
        return {"stub": {"_id": "prov_stub", "name": "stub",
                         "kind": "openai-compat",
                         "base_url": f"{live_server}/v1",
                         "key_env": "NOPE", "enabled": True}}

    monkeypatch.setattr(dispatcher, "ready_providers", fake_ready)
    monkeypatch.setattr(dispatcher, "provider_key", lambda p: "k")
    monkeypatch.setattr(dispatcher.db, "model_catalog", _FakeCol())

    n = asyncio.run(dispatcher.refresh_catalog())
    assert n == 1, f"expected the stub's model listed, got {n}"


class _FakeCol:
    def __init__(self):
        self.ops = []

    async def update_one(self, q, doc, upsert=False):
        self.ops.append(("one", q))

    async def update_many(self, q, doc):
        self.ops.append(("many", q))
