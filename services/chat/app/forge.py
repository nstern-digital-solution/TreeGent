"""Forgejo account provisioning (R80: self-hosted forge for TreeGent).

Every actor created in TreeGent gets a matching Forgejo account,
automatically, via the admin API. Design constraints:

- TOTAL: a down/slow forge must never block or fail actor creation —
  provisioning is best-effort with a pending-retry state on the actor doc.
- IDEMPOTENT: safe to re-run (409 username-exists = success).
- NO NOTIFICATIONS: forge events (PRs, review requests) deliberately do
  NOT become chat messages (ruling 2026-10-15) — accounts only.
- DISABLED BY DEFAULT: TG_CHAT_FORGE_URL empty (default) disables the
  connector entirely; no outbound calls happen.

Config (env, chat service):
  TG_CHAT_FORGE_URL    base url, e.g. https://git.nstern.de   ("" = disabled)
  TG_CHAT_FORGE_TOKEN  admin-scoped API token (required when URL set)
"""
from __future__ import annotations

import asyncio
import re
import secrets as pysecrets

import httpx

from . import db
from .config import settings

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# chars that never appear in generated passwords (ambiguous lookers removed:
# l/I/1, O/0, and lowercase i)
_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def _forge_username(actor: dict) -> str:
    """Forgejo username for an actor: the TreeGent username, lowercased.

    Forgejo rules: [a-z0-9._-], max 40. TreeGent usernames are already
    lowercased ascii at creation (actors.py) but be defensive anyway.
    """
    u = (actor.get("username") or "").lower()
    u = re.sub(r"[^a-z0-9-]", "-", u).strip("-") or "actor"
    return u[:40]


def _gen_password() -> str:
    return "".join(pysecrets.choice(_PW_ALPHABET) for _ in range(28))


async def _api(method: str, path: str, **kw) -> httpx.Response:
    url = settings.forge_url.rstrip("/") + "/api/v1" + path
    headers = {"Authorization": f"token {settings.forge_token}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10, headers=headers) as c:
        return await c.request(method, url, **kw)


async def provision(actor: dict) -> None:
    """Ensure a Forgejo account exists for this actor. Never raises.

    Called fire-and-forget from create_actor; retried by sync_pending()
    for actors whose provisioning failed earlier (forge down etc.).
    """
    if not settings.forge_url:
        return
    try:
        await _provision_locked(actor)
    except Exception as e:  # noqa: BLE001 — total by contract
        try:
            await db.actors.update_one(
                {"_id": actor["_id"]},
                {"$set": {"forge.status": "pending",
                          "forge.error": f"{type(e).__name__}: {str(e)[:200]}"}})
        except Exception:  # noqa: BLE001 — bookkeeping must never break totality
            pass


async def _provision_locked(actor: dict) -> None:
    uname = _forge_username(actor)
    email = actor.get("forge", {}).get("email") or \
        f"{uname}@forge.treegent.invalid"
    r = await _api("POST", "/admin/users", json={
        "username": uname,
        "email": email,
        "password": _gen_password(),
        "must_change_password": False,
        "login_name": actor["_id"],        # traceability: actor id on the forge
        "full_name": actor.get("display_name") or "",   # R81: forge shows the name
        "visibility": "public",
        "send_notify": False,               # no mail out (R67-adjacent)
    })
    if r.status_code == 201:
        await db.actors.update_one(
            {"_id": actor["_id"]},
            {"$set": {"forge.status": "ok",
                      "forge.username": uname}})
        return
    if r.status_code == 422 and "user already exists" in r.text.lower():
        # idempotent re-run (e.g. after a timeout where the create did land)
        await db.actors.update_one(
            {"_id": actor["_id"]},
            {"$set": {"forge.status": "ok",
                      "forge.username": uname}})
        return
    r.raise_for_status()


async def backfill_missing() -> int:
    """Mark every actor without forge state as pending and sync once.

    Runs at chat-service startup (R80): actors created before the connector
    existed — or while the forge was down — get their accounts without any
    manual step. Total and idempotent; no-op when the connector is disabled
    or every actor is already provisioned.
    """
    if not settings.forge_url:
        return 0
    await db.actors.update_many(
        {"forge.status": {"$exists": False}},
        {"$set": {"forge.status": "pending", "forge.error": "backfill"}})
    return await sync_pending()


async def sync_pending() -> int:
    """Retry every actor stuck in forge.status=pending. Returns count fixed.

    Invoked from the chat service's periodic maintenance sweep (if any)
    or on demand via internal route; safe to call repeatedly.
    """
    if not settings.forge_url:
        return 0
    fixed = 0
    async for a in db.actors.find({"forge.status": "pending"}):
        try:
            await _provision_locked(a)
            fixed += 1
        except Exception as e:  # noqa: BLE001
            await db.actors.update_one(
                {"_id": a["_id"]},
                {"$set": {"forge.error": f"{type(e).__name__}: {str(e)[:200]}"}})
    return fixed


async def rename_account(old: str, new: str) -> bool:
    """R81: rename a forge account. Forgejo 12 admin API:
    POST /admin/users/{old}/rename {"new_username": new} -> 204.
    Old handle keeps 307-redirecting, so existing issue/PR references
    keep resolving. Returns True on confirmed rename."""
    if not settings.forge_url:
        return False
    r = await _api("POST", f"/admin/users/{old}/rename",
                   json={"new_username": new})
    return r.status_code in (200, 204)


async def ensure_repo(full_name: str) -> bool:
    """Ensure an org repo exists to mirror a TreeGent project. Future use."""
    return False
