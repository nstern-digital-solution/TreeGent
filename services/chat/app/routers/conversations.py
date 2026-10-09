from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from treegent_common.perms import is_root_actor

from .. import db, idgen, org
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


async def _assert_in_subtree(creator_id: str, actor: dict,
                             member_ids: list[str]) -> None:
    """Issue #22 membership policy: who may hold membership in a channel/
    group is bounded by the conversation creator's ORG SUBTREE (creator +
    descendants, org.py ancestry paths). An agent key therefore cannot
    open a channel that pulls in humans or peers outside its subtree and
    read along silently. Root (the trust anchor of the whole tree) is
    exempt — their subtree is the org itself anyway. Identity comes from
    credentials (R62); this is pure authorization on top of it."""
    if is_root_actor(actor):
        return
    allowed = set(await org.subtree_ids(creator_id))
    outside = [m for m in member_ids if m not in allowed]
    if outside:
        raise HTTPException(
            403, "members must lie within the conversation creator's "
                 "org subtree")


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
    for m in body.member_ids:
        if not await db.actors.find_one({"_id": m}):
            raise HTTPException(400, f"member {m} not found")
    # issue #22: membership policy — requested members must lie inside the
    # creator's org subtree (was: any actor incl. agent keys could pick
    # arbitrary members)
    await _assert_in_subtree(actor["_id"], actor, body.member_ids)
    members = sorted(set(body.member_ids) | {actor["_id"]})
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


class MembersIn(BaseModel):
    member_ids: list[str] = Field(default_factory=list)


@router.post("/{conv_id}/members")
async def add_member(conv_id: str, body: MembersIn,
                     actor: dict = Depends(require_service)):
    c = await db.conversations.find_one({"_id": conv_id})
    if not c:
        raise HTTPException(404, "no such conversation")
    if c["kind"] == "dm":
        raise HTTPException(400, "cannot add members to a DM")
    me = actor["_id"]
    # issue #22: nobody may silently pull OTHERS in. Only the conversation
    # creator or root may add third parties (and only within the creator's
    # org subtree); any other member is limited to re-adding themselves,
    # and a non-member can never join at all (a conversation is not
    # self-service).
    privileged = is_root_actor(actor) or me == c.get("created_by")
    others = [m for m in body.member_ids if m != me]
    if not privileged:
        if me not in c["members"]:
            raise HTTPException(403, "not a member")
        if others:
            raise HTTPException(
                403, "only the conversation creator or root may add members")
    for m in body.member_ids:
        if not await db.actors.find_one({"_id": m}):
            raise HTTPException(400, f"member {m} not found")
    if others:
        await _assert_in_subtree(c.get("created_by") or me, actor, others)
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
    me = actor["_id"]
    # issue #22: nobody may silently pull OTHERS out. Leaving is self-only
    # for any member; only the conversation creator or root may remove
    # third parties (moderation).
    privileged = is_root_actor(actor) or me == c.get("created_by")
    if not privileged:
        if me not in c["members"]:
            raise HTTPException(403, "not a member")
        if member_id != me:
            raise HTTPException(
                403, "only the conversation creator or root may remove members")
    await db.conversations.update_one({"_id": conv_id},
                                      {"$pull": {"members": member_id}})
    return pub(await db.conversations.find_one({"_id": conv_id}))
