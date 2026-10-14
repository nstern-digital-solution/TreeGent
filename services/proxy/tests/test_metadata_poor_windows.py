"""Providers whose /models carries NO context_length.

opencode's model list (https://opencode.ai/zen/v1/models) returns 85 rows of
{id, object, created, owned_by} — no context_length, no architecture.
refresh_catalog therefore stored ctx = null for every one of them,
context_window_for fell through to the 32768 default, and the dashboard's
Context column showed "-" for the whole table. Consequence (Felix, 2026-10-14,
"32k is tiny and lobotomizes you"): the R78 trigger became 16,384 prompt
tokens and the trim target 8,192 — BELOW an agent's fixed prompt overhead
(system prompt + tool schemas measured ~13,700 tokens), so long sessions
re-compact turn after turn and the working set never stabilises.

Fix: at refresh time a missing ctx is filled from the public models.dev
registry (keyed by provider api host + bare model id; 81 of opencode's 85
models match exactly, and the SMALLEST context in that set is 204,800 — no
opencode model is a 32k model). Ranking is unchanged: an admin override still
outranks ctx, and a gateway that reports its own number is never overridden.

Pure-unit: no Mongo, no network.
"""
import asyncio
import json
import os
import sys

os.environ.setdefault("TG_DEV", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

import services.proxy.app.dispatcher as d  # noqa: E402


SNIPPET = {
    "opencode": {
        "id": "opencode", "env": ["OPENCODE_API_KEY"],
        "api": "https://opencode.ai/inference/v1",
        "models": {
            "qwen3.8-flash": {"limit": {"context": 1000000,
                                        "output": 131072}},
            "glm-5.1": {"limit": {"context": 204800, "output": 131072}},
            "zero-ctx": {"limit": {"context": 0}},
            "no-limits": {},
        },
    },
    # same HOST as "opencode", different product line, different limits —
    # must not clobber the first entry for a shared model id
    "opencode-go": {
        "api": "https://opencode.ai/inference/go/v1",
        "models": {"qwen3.8-flash": {"limit": {"context": 262144}},
                   "minimax-m3": {"limit": {"context": 512000}}},
    },
    "hostless": {"models": {"x": {"limit": {"context": 5}}}},
    "no-api": {"models": {"y": {"limit": {"context": 7}}}},
    "not-a-dict": 42,
}


def test_parse_shape_and_host_keying():
    reg = d._parse_models_dev(SNIPPET)
    assert set(reg) == {"opencode.ai"}
    assert reg["opencode.ai"]["qwen3.8-flash"] == 1000000   # first writer wins
    assert reg["opencode.ai"]["minimax-m3"] == 512000       # unique id merges
    assert "zero-ctx" not in reg["opencode.ai"]
    assert "no-limits" not in reg["opencode.ai"]


def test_parse_is_total_on_garbage():
    for bad in ({}, None, [], {"a": None}, {"a": {"api": 1, "models": {}}},
                {"a": {"api": "//not-a-url", "models": {"m": {"limit": {}}}}},
                {"a": {"api": "https://h", "models": {"m": "not-a-dict"}}}):
        assert isinstance(d._parse_models_dev(bad), dict)


def test_bool_context_is_not_a_window():
    """isinstance(True, int) is True — a JSON true must not become a
    1-token window."""
    reg = d._parse_models_dev({"p": {"api": "https://x.test/v1",
                                     "models": {"m": {"limit":
                                                       {"context": True}}}}})
    assert reg == {}


def test_registry_host():
    assert d._host_key("https://opencode.ai/zen") == "opencode.ai"
    assert d._host_key("http://Localhost:8001/v1") == "localhost"
    assert d._host_key("") == ""
    d._host_key("::not a url::")          # never raises


# ---------------- the ctx merge rule (the actual bug) ---------------------

def test_gateway_reported_value_wins():
    """A provider that publishes context_length is never overridden by the
    registry, even when they disagree."""
    assert d._resolved_ctx({"id": "m", "context_length": 262144},
                           {"m": 1000000}, "m") == 262144


def test_registry_fills_the_hole():
    """THE fix: an opencode-style row carries no context_length at all."""
    assert d._resolved_ctx({"id": "qwen3.8-flash"},
                           {"qwen3.8-flash": 1000000},
                           "qwen3.8-flash") == 1000000


def test_missing_everywhere_is_none_not_zero():
    assert d._resolved_ctx({"id": "jev-1.13"}, {}, "jev-1.13") is None
    assert d._resolved_ctx({"context_length": 0}, {}, "m") is None


def test_reported_zero_falls_through_to_registry():
    """A gateway saying 0 is not saying 'zero tokens' — it is not saying
    anything. The registry must still be allowed to fill it."""
    assert d._resolved_ctx({"context_length": 0}, {"m": 204800}, "m") == 204800


def test_admin_override_still_outranks_ctx():
    """#69's ranking is UNTOUCHED: context_window_for prefers the admin
    field over the (now registry-filled) ctx. Pinned with a faked catalog."""
    rows = [{"_id": "opencode/m", "ctx": 1000000, "context_window": 65536}]

    class _Cur:
        def __init__(self, r): self.r, self.i = r, 0
        def __aiter__(self): return self
        async def __anext__(self):
            if self.i >= len(self.r): raise StopAsyncIteration
            v = self.r[self.i]; self.i += 1; return v

    class _Cat:
        def find(self, q, proj): return _Cur(rows)

    d._ctx_windows, d._ctx_loaded_at = {}, 0.0
    d.db.model_catalog = _Cat()
    assert asyncio.run(d.context_window_for("opencode/m")) == 65536
    assert asyncio.run(d.context_window_for("opencode/nothere")) == 32768


# ---------------- fetch behaviour: total + thread-offloaded ---------------

def test_load_models_dev_never_raises(monkeypatch):
    d._models_dev, d._models_dev_loaded_at = {}, 0.0
    monkeypatch.setattr(d.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert asyncio.run(d.load_models_dev()) == {}


def test_load_models_dev_caches_and_uses_keyword_timeout(monkeypatch):
    d._models_dev, d._models_dev_loaded_at = {}, 0.0
    calls = []

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return json.dumps(SNIPPET).encode()

    def fake(req, timeout=None):
        # #54 invariant: a positional timeout binds to `data` and every
        # fetch TypeErrors — the registry call must pass it as a keyword.
        assert timeout is not None, "timeout must be a keyword arg"
        calls.append(1)
        return _R()

    monkeypatch.setattr(d.urllib.request, "urlopen", fake)
    r1 = asyncio.run(d.load_models_dev())
    r2 = asyncio.run(d.load_models_dev())
    assert r1 and r1 is r2
    assert len(calls) == 1                      # cached: one fetch per TTL
