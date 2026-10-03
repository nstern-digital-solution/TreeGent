from fastapi import APIRouter, Depends

from .. import db, idgen

from .. import db
from ..security import require_service_only
from ..security import require_service

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/org-weights")
async def org_weights(_: None = Depends(require_service_only)):
    """Actor id → org depth (0 = top of tree). Used by services/proxy to
    compute R32 priority (hierarchy weight). Service-token only."""
    out = {}
    async for a in db.actors.find({}, {"org.depth": 1}):
        out[a["_id"]] = (a.get("org") or {}).get("depth", 0)
    return out


@router.get("/resolve-actor")
async def resolve_actor(username: str, _: None = Depends(require_service_only)):
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


@router.post("/agent-inbox-delivered")
async def agent_inbox_delivered(body: dict, _: None = Depends(require_service_only)):
    await db.inbox.update_many(
        {"recipient_id": body.get("agent_id"), "delivered_at": None},
        {"$set": {"delivered_at": idgen.now()}})
    return {"ok": True}
