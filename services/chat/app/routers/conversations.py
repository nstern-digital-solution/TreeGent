from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, idgen
from ..security import require_service

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConvIn(BaseModel):
    kind: str = "channel"           # channel | group
    name: str = Field(min_length=1, max_length=48)
    member_ids: list[str] = Field(default_factory=list)


def pub(c: dict) -> dict:
    return {
        "id": c["_id"],
        "kind": c["kind"],
        "name": c["name"],
        "members": c.get("members", []),
        "created_at": c.get("created_at"),
        "created_by": c.get("created_by"),
    }


async def _dm_id(a: str, b: str) -> str:
    x, y = sorted([a, b])
    return f"dm_{x}__{y}"


@router.post("/dm/{other_id}", status_code=201)
async def open_dm(other_id: str, actor: dict = Depends(require_service)):
    me = actor["_id"]
    if other_id == me:
        raise HTTPException(400, "cannot DM yourself")
    other = await db.actors.find_one({"_id": other_id})
    if not other:
        raise HTTPException(404, "no such actor")
    cid = await _dm_id(me, other_id)
    existing = await db.conversations.find_one({"_id": cid})
    if existing:
        return pub(existing)
    doc = {
        "_id": cid,
        "kind": "dm",
        "name": None,
        "members": sorted([me, other_id]),
        "created_at": idgen.now(),
        "created_by": me,
    }
    await db.conversations.insert_one(doc)
    return pub(doc)


@router.post("", status_code=201)
async def create_conv(body: ConvIn, actor: dict = Depends(require_service)):
    if body.kind not in ("channel", "group"):
        raise HTTPException(400, "kind must be channel or group")
    members = sorted(set(body.member_ids) | {actor["_id"]})
    for m in body.member_ids:
        if not await db.actors.find_one({"_id": m}):
            raise HTTPException(400, f"member {m} not found")
    doc = {
        "_id": idgen.new_id("cnv"),
        "kind": body.kind,
        "name": body.name,
        "members": members,
        "created_at": idgen.now(),
        "created_by": actor["_id"],
    }
    await db.conversations.insert_one(doc)
    return pub(doc)


@router.get("")
async def list_convs(actor: dict = Depends(require_service)):
    me = actor["_id"]
    return [pub(c) async for c in db.conversations.find({"members": me})]


@router.post("/{conv_id}/members")
async def add_member(conv_id: str, body: ConvIn,
                     actor: dict = Depends(require_service)):
    c = await db.conversations.find_one({"_id": conv_id})
    if not c:
        raise HTTPException(404, "no such conversation")
    if c["kind"] == "dm":
        raise HTTPException(400, "cannot add members to a DM")
    if actor["_id"] not in c["members"]:
        raise HTTPException(403, "not a member")
    for m in body.member_ids:
        if not await db.actors.find_one({"_id": m}):
            raise HTTPException(400, f"member {m} not found")
    await db.conversations.update_one({"_id": conv_id},
                                      {"$addToSet": {"members": {"$each": body.member_ids}}})
    return pub(await db.conversations.find_one({"_id": conv_id}))


@router.get("/{conv_id}")
async def get_conv(conv_id: str, actor: dict = Depends(require_service)):
    c = await db.conversations.find_one({"_id": conv_id})
    if not c:
        raise HTTPException(404, "no such conversation")
    if actor["_id"] not in c["members"]:
        raise HTTPException(403, "not a member")
    return pub(c)


@router.delete("/{conv_id}/members/{member_id}")
async def remove_member(conv_id: str, member_id: str,
                        actor: dict = Depends(require_service)):
    c = await db.conversations.find_one({"_id": conv_id})
    if not c:
        raise HTTPException(404, "no such conversation")
    if c["kind"] == "dm":
        raise HTTPException(400, "cannot remove members from a DM")
    if actor["_id"] not in c["members"]:
        raise HTTPException(403, "not a member")
    await db.conversations.update_one({"_id": conv_id},
                                      {"$pull": {"members": member_id}})
    return pub(await db.conversations.find_one({"_id": conv_id}))
