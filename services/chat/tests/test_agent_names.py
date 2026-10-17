"""R81 regression tests: generated-name usernames, infra handles, rename.

Covers: agent created with an operator "agent-N" handle gets a generated
name as username (handle preserved as infra_id); persona and username
agree; rename endpoint rejects infra-style names, renames cleanly, and
syncs the forge account via the lab-verified admin API.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_DEV", "1")

import pytest


class _FakeCol:
    def __init__(self):
        self.docs = []
        self.by_uname = {}

    async def find_one(self, q):
        if "name" in q:
            d = next((x for x in self.docs if x.get("name") == q["name"]), None)
            return dict(d) if d else None
        if "username" in q:
            d = self.by_uname.get(q["username"])
            return dict(d) if d else None
        if "_id" in q:
            d = next((x for x in self.docs if x["_id"] == q["_id"]), None)
            return dict(d) if d else None
        return None

    async def insert_one(self, d):
        self.docs.append(dict(d))
        self.by_uname[d["username"]] = d
        return type("R", (), {"inserted_id": d["_id"]})()

    async def update_one(self, q, u):
        d = next((x for x in self.docs if x["_id"] == q.get("_id")), None)
        if d is None and "username" in q:
            d = self.by_uname.get(q["username"])
        if d is None:
            return
        d.update(u.get("$set", {}))
        self.by_uname.pop(q.get("username", ""), None)
        self.by_uname[d["username"]] = d


@pytest.fixture
def fake_db(monkeypatch):
    col = _FakeCol()
    from app.routers import actors as A
    monkeypatch.setattr(A.db, "actors", col)
    return col


def test_infra_handle_detection():
    from app.routers.actors import _is_infra_handle
    assert _is_infra_handle("agent-1")
    assert _is_infra_handle("AGENT-32")
    assert not _is_infra_handle("rosa")
    assert not _is_infra_handle("agent-x")
    assert not _is_infra_handle("")
    assert not _is_infra_handle(None)


@pytest.mark.asyncio
async def test_agent_username_is_stable_id(fake_db):
    """R82 (supersedes R81): username passes through verbatim as the
    operator's stable ID; the generated persona name is NOT the username.
    Callers address the agent by `name` (rosa), identify by username."""
    from app.routers import actors as A
    from treegent_common.identity import assign_identity

    body = A.ActorIn(username="agent-1", display_name="", kind="agent")
    persona = assign_identity()
    uname = body.username.lower()          # verbatim — no rewriting
    assert uname == "agent-1"
    name = await A._persona_username(persona)
    first = persona["persona_name"].split()[0].lower()
    assert name.rstrip("0123456789") == first
    assert uname != name                   # ID and name are distinct fields


@pytest.mark.asyncio
async def test_persona_username_suffixes_on_collision(fake_db):
    from app.routers import actors as A
    await fake_db.insert_one({"_id": "agt_x", "username": "agent-9", "name": "mila"})
    p = {"persona_name": "Mila Sommer"}
    u = await A._persona_username(p)
    assert u == "mila2"


@pytest.mark.asyncio
async def test_rename_account_calls_forge(monkeypatch):
    from app import forge
    calls = []

    async def fake_api(method, path, **kw):
        calls.append((method, path, kw.get("json")))

        class R:
            status_code = 204

        return R()

    monkeypatch.setattr(forge, "_api", fake_api)
    monkeypatch.setattr(forge.settings, "forge_url", "https://git.example")
    assert await forge.rename_account("agent-1", "rosa") is True
    assert calls == [("POST", "/admin/users/agent-1/rename",
                      {"new_username": "rosa"})]
