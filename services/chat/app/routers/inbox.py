from fastapi import APIRouter, Depends

from .. import db
from ..security import require_service

router = APIRouter(prefix="/inbox", tags=["inbox"])


def ipub(i: dict) -> dict:
    return {
        "id": i["_id"],
        "message_id": i["message_id"],
        "conversation_id": i["conversation_id"],
        "recipient_id": i["recipient_id"],
        "received_at": i.get("received_at"),
        "delivered_at": i.get("delivered_at"),
        "delivered_turn": i.get("delivered_turn"),
    }


@router.get("")
async def my_inbox(only_undelivered: bool = True,
                   actor: dict = Depends(require_service)):
    q = {"recipient_id": actor["_id"]}
    if only_undelivered:
        q["delivered_at"] = None
    cur = db.inbox.find(q).sort("_id", 1).limit(500)
    out = []
    async for i in cur:
        m = await db.messages.find_one({"_id": i["message_id"]})
        s = await db.actors.find_one({"_id": m["sender_id"]}) if m else None
        out.append({
            **ipub(i),
            "body": m.get("body") if m else None,
            "sender_id": m.get("sender_id") if m else None,
            "sender_username": (s or {}).get("username") or (m or {}).get("sender_username"),
        })
    return out


@router.post("/deliver")
async def mark_delivered(actor: dict = Depends(require_service)):
    """Mark every undelivered inbox row of the caller as delivered.
    The turn id is opaque for M1; the agent runtime passes a real one later."""
    from datetime import datetime, timezone

    r = await db.inbox.update_many(
        {"recipient_id": actor["_id"], "delivered_at": None},
        {"$set": {"delivered_at": datetime.now(timezone.utc),
                  "delivered_turn": "t1"}},
    )
    return {"delivered": r.modified_count}
