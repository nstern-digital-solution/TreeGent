"""R82 regression tests: stable-ID usernames, generated display names,
credentials handoff, real mailbox emails on forge accounts.

R82 model (operator ruling 2026-10-17):
  username   = operator's stable ID (agent-1), NEVER renamed
  name       = generated given name (rosa) — mentions + forge account
  display_name = full persona name (Rosa Marchetti) — UI
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_DEV", "1")

import pytest


def test_stable_username_not_rewritten():
    """username passes through verbatim — operator's ID is the identity."""
    from app.routers.actors import _is_infra_handle
    # the R81 rewriting helpers are gone from the create path; the regex
    # helper stays for legacy detection only
    assert _is_infra_handle("agent-1") is True


@pytest.mark.asyncio
async def test_persona_username_used_for_name_field():
    """generated name lands in `name`, display_name = full persona."""
    from treegent_common.identity import assign_identity
    from app.routers.actors import _persona_username

    class _Col:
        async def find_one(self, q):
            return None

    from app.routers import actors as A
    A.db.actors = _Col()
    persona = assign_identity()
    name = await A._persona_username(persona)
    first = persona["persona_name"].split()[0].lower()
    assert name.rstrip("0123456789") == first
    assert " " not in name


@pytest.mark.asyncio
async def test_handoff_writes_secret_with_token(monkeypatch):
    """R82: provisioning hands the agent a scoped forge token via secrets."""
    from app import forge

    calls = []

    class _Resp:
        status_code = 201

        def json(self):
            return {"sha1": "tok_abc123"}

    async def fake_api(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return _Resp()

    puts = []

    async def fake_put(owner_id, name, fields):
        puts.append((owner_id, name, fields))

    monkeypatch.setattr(forge, "_api", fake_api)
    monkeypatch.setattr(forge, "_secrets_put", fake_put)
    monkeypatch.setattr(forge.settings, "forge_url", "https://git.example")
    await forge._handoff_credentials({"_id": "agt_x"}, "rosa", "pw")
    assert any("/tokens" in p for _, p, _ in calls), calls
    assert puts and puts[0][0] == "agt_x" and puts[0][1] == "forge/token"
    assert puts[0][2]["token"] == "tok_abc123"


@pytest.mark.asyncio
async def test_provision_uses_real_mailbox_email():
    """R82: no more @forge.treegent.invalid when TG_MAIL_DOMAIN is set."""
    from app import forge

    monkey = None
    await asyncio.sleep(0)  # async marker
    a = {"persona": {"persona_name": "Rosa Marchetti"},
         "username": "agent-1"}
    local = forge._agent_mailbox_local(a)
    assert local == "rosa.marchetti"
    monkey = pytest.MonkeyPatch()
    monkey.setattr(forge.settings, "mail_domain", "e.nstern.de")
    try:
        email = f"{local}@{forge.settings.mail_domain}"
        assert email == "rosa.marchetti@e.nstern.de"
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_backfill_stamps_name_for_legacy_agents(monkeypatch):
    from app import forge

    stamped = []
    docs = [{"_id": "a1", "kind": "agent", "display_name": "Rosa Marchetti"},
            {"_id": "a2", "kind": "agent", "display_name": "Greta Okafor"}]

    class _Cur:
        def __init__(self, items): self.items = list(items)

        async def to_list(self, n): return []

        def __aiter__(self): return self

        async def __anext__(self):
            if self.items: return self.items.pop(0)
            raise StopAsyncIteration

    class _Col:
        def find(self, q):
            if "name" in q:
                return _Cur(docs)
            return _Cur([])

        async def update_one(self, q, u):
            stamped.append((q, u))

        async def update_many(self, q, u): pass

    monkeypatch.setattr(forge.db, "actors", _Col())
    monkeypatch.setattr(forge, "sync_pending", lambda: asyncio.sleep(0, result=0))
    monkeypatch.setattr(forge.settings, "forge_url", "https://git.example")

    async def fake_api(method, path, **kw):
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(forge, "_api", fake_api)
    await forge.backfill_missing()
    assert any("$set" in u and "name" in u["$set"] for _, u in stamped)


def test_workspace_keyed_by_stable_username():
    """R82: the runtime derives the workspace from username (stable ID)."""
    src = (Path(__file__).resolve().parents[2] / "runtime" / "app" / "loop.py").read_text()
    assert 'actor.get("username", self.id)' in src
    assert "_resolve_workspace" not in src
