from datetime import datetime
from fastapi import APIRouter, Body, Depends, Header, HTTPException

from .. import db, idgen

from .. import db
from ..security import require_service_only
from ..security import require_service

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/org-weights")
async def org_weights(_: None = Depends(require_service)):
    """Actor id → org depth (0 = top of tree). Used by services/proxy to
    compute R32 priority (hierarchy weight). Service-token only."""
    out = {}
    async for a in db.actors.find({}, {"org.depth": 1}):
        out[a["_id"]] = (a.get("org") or {}).get("depth", 0)
    return out


@router.get("/resolve-actor")
async def resolve_actor(username: str, _: None = Depends(require_service)):
    a = await db.actors.find_one({"username": username}, {"_id": 1})
    return {"actor_id": a["_id"] if a else None}


@router.get("/agent-inbox")
async def agent_inbox(agent_id: str, _: None = Depends(require_service_only)):
    """Undelivered chat messages for an agent, oldest first."""
    out = []
    async for row in db.inbox.find(
            {"recipient_id": agent_id, "delivered_at": None}
            ).sort("received_at", 1).limit(10):
        msg = await db.messages.find_one({"_id": row["message_id"]})
        if not msg:
            continue
        sender = await db.actors.find_one({"_id": msg["sender_id"]})
        out.append({"message_id": msg["_id"],
                    "sender_username": (sender or {}).get("username", "?"),
                    "body": msg.get("body", ""),
                    "received_at": row.get("received_at")})
    return {"messages": out}


def _flag(v, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return default


async def _fetch_agent_messages(agent_id: str, sender: str | None,
                                unread: bool, read: bool,
                                since: str | None, limit: int) -> dict:
    """Shared chat.check backend (internal + host tier)."""
    from .. import db
    """chat.check backend: an agent's inbox with filters — sender, read
    state, since-ts, limit. Marks returned rows delivered (the agent has
    SEEN them); content is pulled ON DEMAND, never pushed (R14 async)."""
    q: dict = {"recipient_id": agent_id}
    if not (unread or read):
        return {"messages": []}
    if unread and not read:
        q["delivered_at"] = None
    elif read and not unread:
        q["delivered_at"] = {"$ne": None}
    if sender:
        s = await db.actors.find_one({"username": sender}, {"_id": 1})
        if not s:
            return {"messages": []}
        q["sender_id"] = s["_id"]
    cur = db.inbox.find(q).sort("received_at", -1).limit(max(1, min(limit, 100)))
    out = []
    async for row in cur:
        msg = await db.messages.find_one({"_id": row["message_id"]})
        if not msg:
            continue
        if since and (row.get("received_at") or "") < since:
            continue
        sname = (await db.actors.find_one(
            {"_id": msg["sender_id"]})) if sender is None else None
        out.append({"message_id": msg["_id"],
                    "sender_username": (sname or {}).get("username", "?")
                    if sname else sender or "?",
                    "body": msg.get("body", ""),
                    "received_at": row.get("received_at"),
                    "delivered": bool(row.get("delivered_at"))})
    # the agent has now SEEN these — mark delivered (async contract: the
    # notification was already injected; content retrieval completes it)
    ids = [row["_id"] for row in
           await db.inbox.find(q).sort("received_at", -1)
           .limit(max(1, min(limit, 100))).to_list(None)]
    if ids:
        await db.inbox.update_many({"_id": {"$in": ids},
                                    "delivered_at": None},
                                   {"$set": {"delivered_at": idgen.now()}})
    out.reverse()   # oldest first for reading
    return {"messages": out}


@router.get("/agent-messages")
async def agent_messages(agent_id: str, sender: str | None = None,
                         unread: str = "true", read: str = "true",
                         since: str | None = None, limit: int = 20,
                         _: None = Depends(require_service_only)):
    return await _fetch_agent_messages(
        agent_id, sender=sender, unread=_flag(unread), read=_flag(read),
        since=since, limit=limit)


@router.post("/agent-inbox-delivered")
async def agent_inbox_delivered(body: dict, _: None = Depends(require_service_only)):
    await db.inbox.update_many(
        {"recipient_id": body.get("agent_id"), "delivered_at": None},
        {"$set": {"delivered_at": idgen.now()}})
    return {"ok": True}


@router.post("/agents/{agent_id}/keys")
async def issue_agent_key(agent_id: str, actor: dict = Depends(require_service)):
    """Issue a fresh agent key (R45). Central tier only (the web calls this
    for an admin); agent tier can never mint keys for anyone. Returns the
    key ONCE — it is never retrievable again."""
    if actor.get("kind") not in ("human", None):
        raise HTTPException(403, "humans only")
    a = await db.actors.find_one({"_id": agent_id})
    if not a or a.get("kind") != "agent":
        raise HTTPException(404, "no such agent actor")
    import secrets
    key = f"sk-agt-{secrets.token_urlsafe(24)}"
    await db.agent_keys.insert_one({"_id": key, "agent_id": agent_id,
                                    "created_at": idgen.now(), "revoked": False})
    return {"key": key, "agent_id": agent_id}


@router.patch("/agents/{agent_id}/host")
async def set_agent_host(agent_id: str, body: dict = Body(...),
                         _: None = Depends(require_service_only)):
    """R55: move an agent between hosts (null = central)."""
    host_id = body.get("host_id")  # explicit null must survive (back to central)
    if host_id is not None:
        hosts = db.db.agent_hosts if hasattr(db, "db") else db.agent_hosts
        exists = await hosts.find_one({"_id": host_id}, {"_id": 1})
        if not exists:
            raise HTTPException(404, "no such host")
    r = await db.actors.update_one(
        {"_id": agent_id, "kind": "agent"},
        {"$set": {"host_id": host_id,
                  "updated_at": datetime.utcnow().isoformat() + "Z"}})
    if r.modified_count == 0 and r.matched_count == 0:
        raise HTTPException(404, "no such agent")
    return {"ok": True, "agent_id": agent_id, "host_id": host_id}


@router.delete("/agents/{agent_id}/keys")
async def revoke_agent_keys(agent_id: str, actor: dict = Depends(require_service)):
    if actor.get("kind") not in ("human", None):
        raise HTTPException(403, "humans only")
    r = await db.agent_keys.update_many(
        {"agent_id": agent_id, "revoked": {"$ne": True}},
        {"$set": {"revoked": True, "revoked_at": idgen.now()}})
    return {"revoked": r.modified_count}
