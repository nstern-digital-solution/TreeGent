"""R82 review-blocker pins: rename endpoint gone, name-field collision,
forge mirror = generated name, resolve-actor by name."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_DEV", "1")

import pytest


def test_rename_endpoint_removed():
    """R82 blocker 1: the R81 rename endpoint is retired — gone from source."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers"
           / "actors.py").read_text()
    assert "/username\"" not in src.replace('actor_id}/username', '')
    assert "renamed_from" not in src
    assert "rename_account" not in src


@pytest.mark.asyncio
async def test_name_collision_checked_against_name_field():
    """R82 risk: two rosas must not both pass — uniqueness is on `name`."""
    from app.routers import actors as A

    class _Col:
        def __init__(self, taken): self.taken = set(taken)

        async def find_one(self, q):
            if "name" in q:
                return {"_id": "x"} if q["name"] in self.taken else None
            return None

    A.db.actors = _Col({"rosa"})
    u = await A._persona_username({"persona_name": "Rosa Marchetti"})
    assert u == "rosa2"


def test_forge_username_mirrors_generated_name():
    from app import forge
    assert forge._forge_username({"name": "rosa", "username": "agent-1"}) == "rosa"
    assert forge._forge_username({"username": "agent-1"}) == "agent-1"


@pytest.mark.asyncio
async def test_resolve_actor_by_name_then_username(monkeypatch):
    """R82 risk: runtime DMs address agents by generated name."""
    from app.routers import internal as I

    queries = []

    class _Col:
        async def find_one(self, q, *a):
            queries.append(q)
            if q == {"name": "rosa"}:
                return {"_id": "agt_1"}
            return None

    monkeypatch.setattr(I.db, "actors", _Col())
    # name hit first
    r = await I.resolve_actor("rosa", None)
    assert queries[0] == {"name": "rosa"}
    # username fallback
    queries.clear()

    class _Col2(_Col):
        async def find_one(self, q, *a):
            queries.append(q)
            if q == {"username": "agent-1"}:
                return {"_id": "agt_1"}
            return None

    monkeypatch.setattr(I.db, "actors", _Col2())
    r = await I.resolve_actor("agent-1", None)
    assert queries[-1] == {"username": "agent-1"}


def test_workspace_legacy_adoption():
    """R82 blocker 4: agent finds a dir keyed by its generated name and
    adopts it — no silent amnesia after the R81 experiment."""
    import ast as _ast
    src = (Path(__file__).resolve().parents[2] / "runtime" / "app"
           / "loop.py").read_text()
    tree = _ast.parse(src)
    has = any(
        isinstance(n, _ast.If) for n in _ast.walk(tree))
    assert "legacy" in src and 'actor.get("name")' in src
    assert "os.path.isdir(legacy)" in src
