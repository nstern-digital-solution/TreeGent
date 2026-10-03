"""Secrets API. R40 enforced via treegent-common: listings/search are
own-scope (own + member) and metadata-only; reading the VALUE of one named
secret allows 'superior' reach-down; writes need own/member. Values are
Fernet-encrypted at rest and only decrypted for an authorized read."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from treegent_common.auth import authenticate
from pydantic import BaseModel, Field
from treegent_common.perms import can, principal_for, resource_for

from . import crypto
from .config import db, secrets_col, settings

router = APIRouter(tags=["secrets"])


async def caller_actor(
    x_agent_key: str = Header(default=""),
    x_service_token: str = Header(default=""),
    x_actor_id: str = Header(default=""),
) -> dict:
    """R45: agent key derives identity (claims ignored); central tier =
    shared token + declared actor (web server only)."""
    return await authenticate(db, settings.service_token,
                              x_agent_key, x_service_token, x_actor_id)


async def require_service(x_service_token: str = Header(default="")) -> None:
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")


def now():
    return datetime.now(timezone.utc)


def spub_meta(s: dict) -> dict:
    """Metadata only — value never leaves the store except via /value."""
    return {"id": s["_id"], "name": s["name"], "username": s.get("username"),
            "url": s.get("url"), "notes": s.get("notes"),
            "owner": s["owner"], "shared_with": s.get("shared_with", []),
            "created_at": s.get("created_at"), "updated_at": s.get("updated_at")}


def resource_of(s: dict) -> dict:
    return {"owners": [s["owner"]], "members": s.get("shared_with", []),
            "ancestors": s.get("_owner_ancestors", [])}


async def _with_ancestors(s: dict) -> dict:
    r = await resource_for(db, [s["owner"]], s.get("shared_with", []))
    s["_owner_ancestors"] = r["ancestors"]
    return s


# ---------------- list / search (own-scope, metadata-only) ----------------

@router.get("/secrets")
async def list_secrets(CLAIM_CALLER: str = "", q: str = "", _c: dict = Depends(caller_actor)):
    """Own-scope listing/search by name/username/url — NEVER returns
    values, NEVER returns items the caller merely has reach-down rights
    to (R40: superiors search their own secrets, not subordinates')."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    p = await principal_for(db, caller_id)
    query: dict = {"$or": [{"owner": caller_id},
                           {"shared_with": caller_id}]}
    if q:
        rx = {"$regex": q, "$options": "i"}
        query = {"$and": [query,
                          {"$or": [{"name": rx}, {"username": rx},
                                   {"url": rx}, {"notes": rx}]}]}
    out = []
    async for s in secrets_col.find(query).sort("updated_at", -1).limit(200):
        out.append(spub_meta(s))
    return out


# ---------------- create / update / delete ----------------

class SecretIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=50000)
    username: str = ""
    url: str = ""
    notes: str = ""
    shared_with: list[str] = []


@router.post("/secrets", status_code=201,
             )
async def create_secret(body: SecretIn, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    # sharing grants access to the named actor's own scope
    doc = {
        "_id": f"sec_{abs(hash((caller_id, body.name, now().isoformat()))) % 10**16:016d}",
        "name": body.name, "username": body.username, "url": body.url,
        "notes": body.notes, "value_enc": crypto.encrypt(body.value),
        "owner": caller_id, "shared_with": list(body.shared_with),
        "created_at": now(), "updated_at": now(),
    }
    await secrets_col.insert_one(doc)
    return spub_meta(doc)


@router.put("/secrets/{sec_id}")
async def update_secret(sec_id: str, body: SecretIn, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    s = await secrets_col.find_one({"_id": sec_id})
    if not s:
        raise HTTPException(404, "no such secret")
    p = await principal_for(db, caller_id)
    if not await can(db, p, "secrets.write",
                     (await _with_ancestors(s)) and resource_of(s)):
        raise HTTPException(403, "not allowed to modify this secret")
    upd = {"name": body.name, "username": body.username, "url": body.url,
           "notes": body.notes, "updated_at": now()}
    if body.value:
        upd["value_enc"] = crypto.encrypt(body.value)
    # sharing changes only by owner/member
    upd["shared_with"] = list(body.shared_with)
    await secrets_col.update_one({"_id": sec_id}, {"$set": upd})
    s.update(upd)
    return spub_meta(s)


@router.delete("/secrets/{sec_id}")
async def delete_secret(sec_id: str, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    s = await secrets_col.find_one({"_id": sec_id})
    if not s:
        raise HTTPException(404, "no such secret")
    p = await principal_for(db, caller_id)
    # only the owner deletes (strictest write)
    if caller_id != s["owner"]:
        raise HTTPException(403, "only the owner may delete a secret")
    await secrets_col.delete_one({"_id": sec_id})
    return {"deleted": sec_id}


# ---------------- value read (named secret, reach-down allowed) ----------

@router.get("/secrets/{sec_id}/value")
async def read_value(sec_id: str, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
    """The ONE endpoint that decrypts. Rule secrets.read allows own/member/
    superior — a superior reads a named subordinate secret here, never
    via search."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    s = await secrets_col.find_one({"_id": sec_id})
    if not s:
        raise HTTPException(404, "no such secret")
    p = await principal_for(db, caller_id)
    await _with_ancestors(s)
    if not await can(db, p, "secrets.read", resource_of(s)):
        raise HTTPException(403, "not allowed to read this secret")
    return {"id": sec_id, "name": s["name"], "value": crypto.decrypt(s["value_enc"])}
