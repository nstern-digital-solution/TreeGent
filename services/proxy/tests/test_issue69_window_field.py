"""Issue #69: refresh_catalog writes `ctx` (provider context_length) but
context_window_for read only `context_window` — so a completed refresh
preseeded NOTHING and every model silently fell back to 32768 (R78
compaction ~4x too often; the 25% target unreachable below prompt
overhead).

Fix: the lookup ranks context_window (admin override, wins) over ctx
(provider-reported preseed). These tests pin the ranking + fallback with
a faked model_catalog; no live Mongo needed.
"""
import asyncio
import os
import sys

os.environ.setdefault("TG_DEV", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))


class _FakeCatalog:
    def __init__(self, rows):
        self.rows = rows

    def find(self, q, proj):
        return _Cur(self.rows)


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        self.i = 0
        return self

    async def __anext__(self):
        if self.i >= len(self.rows):
            raise StopAsyncIteration
        r = self.rows[self.i]
        self.i += 1
        return r


def _dispatcher_with(rows):
    import services.proxy.app.dispatcher as d
    d._ctx_windows = {}
    d._ctx_loaded_at = 0.0            # force reload
    d.db.model_catalog = _FakeCatalog(rows)
    return d


def test_ctx_preseed_used_when_no_override():
    d = _dispatcher_with([
        {"_id": "prov/big-model", "ctx": 131072},
    ])
    assert asyncio.run(d.context_window_for("prov/big-model")) == 131072


def test_admin_override_beats_ctx():
    d = _dispatcher_with([
        {"_id": "prov/m", "ctx": 131072, "context_window": 65536},
    ])
    # manual override wins even when smaller — an admin pinned it
    assert asyncio.run(d.context_window_for("prov/m")) == 65536


def test_bad_values_fall_through():
    d = _dispatcher_with([
        {"_id": "prov/negative", "context_window": -1, "ctx": 8192},
        {"_id": "prov/string", "context_window": "big", "ctx": 0},
        {"_id": "prov/none", "ctx": None},
    ])
    assert asyncio.run(d.context_window_for("prov/negative")) == 8192
    assert asyncio.run(d.context_window_for("prov/string")) == 32768
    assert asyncio.run(d.context_window_for("prov/none")) == 32768
    assert asyncio.run(d.context_window_for("prov/unknown")) == 32768


def test_cache_does_not_resurrect_stale_values():
    d = _dispatcher_with([
        {"_id": "prov/m", "ctx": 131072},
    ])
    assert asyncio.run(d.context_window_for("prov/m")) == 131072
    # admin sets an override; cache reload (TTL forces it via _loaded_at=0
    # reset) must pick it up, not serve the stale preseed
    d.db.model_catalog = _FakeCatalog([
        {"_id": "prov/m", "ctx": 131072, "context_window": 262144},
    ])
    d._ctx_loaded_at = 0.0
    assert asyncio.run(d.context_window_for("prov/m")) == 262144
