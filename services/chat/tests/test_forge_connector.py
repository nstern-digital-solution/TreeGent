"""R80 Forgejo connector tests: mocked forge, real chat app code paths.

Pins the contract from the ruling (2026-10-15):
- every actor created -> forge account (username mirrors, email .invalid)
- connector total: forge down never fails actor creation (pending state)
- idempotent: 422 user-exists counts as provisioned
- empty TG_CHAT_FORGE_URL = connector disabled, zero outbound calls
- internal /forge/sync retries pending actors
- NO forge event -> chat message bridging exists (accounts only)
"""
import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TG_DEV", "1")

from app import forge  # noqa: E402
from app.config import settings  # noqa: E402


class FakeForge:
    """In-process forgejo admin API stub."""

    def __init__(self):
        self.users = {}
        self.fail_with = None   # Exception subclass to raise on next calls

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            raise self.fail_with("forge down")
        if request.method == "POST" and request.url.path == "/api/v1/admin/users":
            import json
            body = json.loads(request.content)
            if body["username"] in self.users:
                return httpx.Response(422, json={"message": "user already exists"})
            self.users[body["username"]] = body
            return httpx.Response(201, json=body)
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def fake_forge(monkeypatch):
    ff = FakeForge()
    # route every _api call through the in-process transport
    def fake_client(**_kw):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(ff.handler),
            base_url="http://forge.test")
    monkeypatch.setattr(forge, "_api", None)  # marker; we patch below instead
    async def api(method, path, **kw):
        c = fake_client()
        try:
            return await c.request(method, "http://forge.test/api/v1" + path, **kw)
        finally:
            await c.aclose()
    monkeypatch.setattr(forge, "_api", api)
    monkeypatch.setattr(settings, "forge_url", "http://forge.test", raising=False)
    monkeypatch.setattr(settings, "forge_token", "t0k", raising=False)
    return ff


def test_username_mapping():
    # R82: forge account mirrors the GENERATED name; stable ID fallback
    assert forge._forge_username({"name": "Rosa", "username": "agent-1"}) == "rosa"
    assert forge._forge_username({"username": "agent_9"}) == "agent-9"  # legacy: no name
    assert forge._forge_username({"username": ""}) == "actor"
    u = forge._forge_username({"username": "x" * 60})
    assert len(u) == 40 and u == "x" * 40


def test_password_alphabet():
    for _ in range(5):
        pw = forge._gen_password()
        assert len(pw) == 28
        assert not set(pw) & set("ilIO01")   # no ambiguous chars


@pytest.mark.asyncio
async def test_provision_accepts_internal_doc_shape_r11(fake_forge):
    """R11: the create_actor hook passes the FULL internal document.
    pub() renames _id->id; provision must read _id from the internal shape
    and must not silently no-op."""
    from app.routers.actors import pub
    internal = {"_id": "agt_r11", "username": "r11-agent",
                "display_name": "R11", "kind": "agent",
                "org": {}, "extra": {}}
    public_shape = pub(internal)   # what the OLD hook passed
    assert "_id" not in public_shape and public_shape["id"] == "agt_r11"
    await forge.provision(internal)          # internal shape: must work
    assert "r11-agent" in fake_forge.users


@pytest.mark.asyncio
async def test_provision_creates_user(fake_forge):
    forge.settings.mail_domain = "e.nstern.de"   # R82: real mailbox domain
    actor = {"_id": "agt_1", "name": "rosa-m", "username": "agent-1", "forge": {}}
    await forge.provision(actor)
    assert "rosa-m" in fake_forge.users
    body = fake_forge.users["rosa-m"]
    assert "@" in body["email"] and "treegent.invalid" not in body["email"]  # R82: real mailbox domain
    assert body["must_change_password"] is False
    assert body["send_notify"] is False
    assert body["login_name"] == "agt_1"


@pytest.mark.asyncio
async def test_provision_idempotent_on_exists(fake_forge, monkeypatch):
    calls = []
    writes = []
    orig = fake_forge.handler

    async def counting(request):
        calls.append(request.url.path)
        return await orig(request)
    fake_forge.handler = counting

    async def cap_update(q, u):
        writes.append((q, u))

    monkeypatch.setattr(forge.db, "actors", type("C", (), {
        "update_one": staticmethod(cap_update)}))

    forge.settings.mail_domain = "e.nstern.de"   # R82: real mailbox domain
    actor = {"_id": "agt_1", "name": "rosa-m", "username": "agent-1", "forge": {}}
    await forge.provision(actor)
    await forge.provision(actor)     # second run: 422 -> success
    # R82: first run also mints a token — count only user creates
    assert len([c for c in calls if c.endswith("/admin/users")]) == 2
    # R82 mint contract: with no admin pw and no compose dir the handoff
    # records pending("no mint path"); the mint itself is pinned in
    # test_forge_mint_contract.py (official /users/{u}/tokens route).
    assert "rosa-m" in fake_forge.users
    assert all(w[1]["$set"].get("forge.status", "ok") == "ok" for w in writes)
    # 2 provision updates + R82 handoff bookkeeping per run
    assert len([w for w in writes if "forge.status" in w[1]["$set"]]) == 2


@pytest.mark.asyncio
async def test_provision_total_on_forge_down(fake_forge, monkeypatch):
    fake_forge.fail_with = httpx.ConnectError

    # actor doc capture: patch db.actors.update_one
    writes = []

    class FakeCol:
        async def update_one(self, q, u):
            writes.append((q, u))
    monkeypatch.setattr(forge.db, "actors", FakeCol())

    actor = {"_id": "agt_2", "username": "felix", "forge": {}}
    await forge.provision(actor)     # must NOT raise
    assert writes and writes[0][1]["$set"]["forge.status"] == "pending"


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "forge_url", "", raising=False)
    calls = []

    async def boom(*a, **k):
        calls.append(a)
        raise AssertionError("must not call the forge when disabled")
    monkeypatch.setattr(forge, "_provision_locked", boom)
    await forge.provision({"_id": "a", "username": "x", "forge": {}})
    assert calls == []


@pytest.mark.asyncio
async def test_sync_pending_retries(fake_forge, monkeypatch):
    monkeypatch.setattr(settings, "forge_url", "http://forge.test", raising=False)

    fixed = []
    pending = [{"_id": "agt_3", "username": "late", "forge":
                {"status": "pending"}}]

    class FakeCursor:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if pending:
                return pending.pop(0)
            raise StopAsyncIteration

    async def _noop(q, u):
        return None

    monkeypatch.setattr(forge.db, "actors", type("C", (), {
        "find": staticmethod(lambda q: FakeCursor()),
        "update_one": staticmethod(_noop)}))

    async def fake_locked(a):
        fixed.append(a["username"])
        return None
    monkeypatch.setattr(forge, "_provision_locked", fake_locked)
    n = await forge.sync_pending()
    assert n == 1 and fixed == ["late"]
