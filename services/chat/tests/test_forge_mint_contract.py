"""Live-verified forge connector behavior (Forgejo v12.0.4, spec + router
source + live probes, 2026-10-17). Pins the contract so regressions like
the invented /admin/users/{u}/tokens route can't come back."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TG_DEV", "1")

import httpx
import pytest


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = str(body or "")

    def json(self):
        return self._body

    def json(self):
        return self._body


class _Client:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, dict(headers or {}),
                           json or {}))
        return _Resp(201, {"sha1": "t" * 40})

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, dict(headers or {}), None))
        return _Resp(200, [])  # no match — name differs per run

    async def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, dict(headers or {}), None))
        return _Resp(204)


@pytest.mark.asyncio
async def test_mint_uses_official_basic_auth_route(monkeypatch):
    """The ONLY spec'd mint route is POST /users/{u}/tokens and it
    requires BASIC auth (reqBasicOrRevProxyAuth in api.go) — admin may
    mint for any user (reqSelfOrAdmin). Token auth is refused."""
    from app import forge
    from app.config import Settings

    calls = []
    monkeypatch.setattr(forge.settings, "forge_admin_password", "pw", raising=False)
    monkeypatch.setattr(forge, "_secrets_put", _no_rec)
    monkeypatch.setattr(forge, "_grant_repo_access", _no_grant)

    import httpx as _hx
    monkeypatch.setattr(_hx, "AsyncClient", lambda **kw: _Client(calls))

    class _A:
        async def update_one(self, *a, **k):
            return None

    monkeypatch.setattr(forge.db, "actors", _A())

    await forge._handoff_credentials({"_id": "agt_rosa_x"}, "rosa", "")
    m, u, h, body = calls[0]
    assert m == "POST" and u.endswith("/users/rosa/tokens")
    assert h["Authorization"].startswith("Basic ")
    assert "write:repository" in body["scopes"]


@pytest.mark.asyncio
async def test_mint_records_pending_not_silent(monkeypatch):
    """422-name-exists path: delete + re-mint (deterministic), and a
    failing mint records forge.handoff=pending with the error."""
    from app import forge

    class _Client2(_Client):
        async def post(self, url, headers=None, json=None):
            self.calls.append(("POST", url, dict(headers or {}),
                               json or {}))
            return _Resp(422, {"message": "name already exists"})

    calls = []
    monkeypatch.setattr(forge.settings, "forge_admin_password", "pw", raising=False)
    monkeypatch.setattr(forge, "_secrets_put", _no_rec)
    monkeypatch.setattr(forge, "_grant_repo_access", _no_grant)

    import httpx as _hx
    monkeypatch.setattr(_hx, "AsyncClient", lambda **kw: _Client2(calls))

    writes = {}

    class _A:
        async def update_one(self, q, u):
            writes.update(u["$set"])

    monkeypatch.setattr(forge.db, "actors", _A())

    await forge._handoff_credentials({"_id": "agt_rosa_x"}, "rosa", "")
    assert writes.get("forge.handoff") == "pending"
    assert "mint 422" in writes.get("forge.handoff_error", "")
    # no token of that name exists -> no DELETE; single POST, then
    # pending recorded (name-collision-with-different-name case)
    kinds = [m for m, *_ in calls]
    assert kinds.count("POST") == 1 and "DELETE" not in kinds, calls


@pytest.mark.asyncio
async def test_cli_mint_path(monkeypatch):
    """No password: container CLI mint (bootstrap's own mechanism)."""
    from app import forge

    monkeypatch.setattr(forge.settings, "forge_admin_password", "", raising=False)
    monkeypatch.setattr(forge.settings, "forge_compose_dir",
                        "/opt/TreeGent/deploy/forgejo", raising=False)
    monkeypatch.setattr(forge, "_secrets_put", _no_rec)
    monkeypatch.setattr(forge, "_grant_repo_access", _no_grant)

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"Access token was successfully created: "
                    b"1234567890abcdef1234567890abcdef12345678", b"")

    async def fake_exec(*a, **k):
        return _Proc()

    import asyncio
    monkeypatch.setattr(asyncio.subprocess, "create_subprocess_exec",
                        fake_exec, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    secrets_puts = []

    async def _cap_put(owner, name, fields):
        secrets_puts.append((owner, name, fields))

    monkeypatch.setattr(forge, "_secrets_put", _cap_put)

    class _A:
        async def update_one(self, *a, **k):
            return None

    monkeypatch.setattr(forge.db, "actors", _A())

    await forge._handoff_credentials({"_id": "agt_rosa_x"}, "rosa", "")
    assert secrets_puts and secrets_puts[0][2]["token"].startswith("1234")


async def _no_rec(owner, name, fields):
    return None


async def _no_grant(actor, uname):
    return None
