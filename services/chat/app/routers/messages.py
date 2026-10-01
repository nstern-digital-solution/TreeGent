import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, idgen
from ..security import require_service

router = APIRouter(prefix="/conversations", tags=["messages"])

MENTION = re.compile(r"@([a-z0-9_.-]{2,32})", re.IGNORECASE)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


def mpub(m: dict, sender: dict | None = None) -> dict:
    out = {
        "id": m["_id"],
        "conversation_id": m["conversation_id"],
        "sender_id": m["sender_id"],
        "sender_username": m.get("sender_username"),
        "body": m["body"],
        "created_at": m.get("created_at"),
    }
    return out


async def _mentioned_agents(conv: dict, body: str) -> list[str]:
    """R3: in channels/groups only @mentioned agents wake; DMs wake all."""
    names = set(MENTION.findall(body))
    if not names:
        return []
    out = []
    async for a in db.actors.find({"kind": "agent", "username": {"$in": list(names)}}):
        if a["_id"] in conv["members"]:
            out.append(a["_id"])
    return out


async def _dispatch(conv: dict, msg: dict) -> int:
    """Fan out to member inboxes + create wake events per R3."""
    wake: set[str] = set()
    if conv["kind"] == "dm":
        wake = {m for m in conv["members"] if m != msg["sender_id"]}
    else:
        for a in await _mentioned_agents(conv, msg["body"]):
            wake.add(a)
    wake = {w for w in wake if w != msg["sender_id"]}
    ops = []
    for m in conv["members"]:
        if m == msg["sender_id"]:
            continue
        ops.append(db.inbox.insert_one({
            "_id": idgen.oid(),
            "message_id": msg["_id"],
            "conversation_id": conv["_id"],
            "recipient_id": m,
            "received_at": msg["created_at"],
            "delivered_at": None,
            "delivered_turn": None,
        }))
    for w in wake:
        kinds = [a async for a in db.actors.find({"_id": w}, {"kind": 1})]
        if kinds and kinds[0].get("kind") == "agent":
            ops.append(db.wake_events.insert_one({
                "_id": idgen.oid(),
                "agent_id": w,
                "reason": "dm" if conv["kind"] == "dm" else "mention",
                "message_id": msg["_id"],
                "created_at": idgen.now(),
                "consumed": False,
            }))
    for op in ops:
        await op
    return len(wake)


@router.post("/{conv_id}/messages", status_code=201)
async def send_message(conv_id: str, body: MessageIn,
                       actor: dict = Depends(require_service)):
    conv = await db.conversations.find_one({"_id": conv_id})
    if not conv:
        raise HTTPException(404, "no such conversation")
    if actor["_id"] not in conv["members"]:
        raise HTTPException(403, "not a member")
    msg = {
        "_id": idgen.oid(),
        "conversation_id": conv_id,
        "sender_id": actor["_id"],
        "sender_username": actor["username"],
        "body": body.body,
        "created_at": idgen.now(),
    }
    await db.messages.insert_one(msg)
    wakes = await _dispatch(conv, msg)
    return {**mpub(msg), "wakes_created": wakes}


@router.get("/{conv_id}/messages")
async def history(conv_id: str, before: str | None = None, limit: int = 100,
                  actor: dict = Depends(require_service)):
    conv = await db.conversations.find_one({"_id": conv_id})
    if not conv:
        raise HTTPException(404, "no such conversation")
    if actor["_id"] not in conv["members"]:
        raise HTTPException(403, "not a member")
    q: dict = {"conversation_id": conv_id}
    if before:
        q["_id"] = {"$lt": before}
    cur = db.messages.find(q).sort("_id", -1).limit(min(limit, 500))
    out = [mpub(m) async for m in cur]
    out.reverse()
    return out
