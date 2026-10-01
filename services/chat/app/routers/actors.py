from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, idgen, org
from ..security import require_service

router = APIRouter(prefix="/actors", tags=["actors"])

KINDS = ("human", "agent")


class ActorIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=64)
    kind: str = "human"
    parent_id: str | None = None
    extra: dict = Field(default_factory=dict)


class ParentIn(BaseModel):
    parent_id: str | None = None


def pub(a: dict) -> dict:
    return {
        "id": a["_id"],
        "username": a["username"],
        "display_name": a["display_name"],
        "kind": a["kind"],
        "org": a.get("org", {"parent_id": None, "ancestors": [], "depth": 0}),
        "created_at": a.get("created_at"),
        "extra": a.get("extra", {}),
    }


@router.post("", status_code=201)
async def create_actor(body: ActorIn, actor: dict = Depends(require_service)):
    if body.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    uname = body.username.lower()
    if await db.actors.find_one({"username": uname}):
        raise HTTPException(409, "username taken")
    node = await org.compute_node("__new__", body.parent_id)  # validates parent
    aid = idgen.new_id("agt" if body.kind == "agent" else "hum")
    doc = {
        "_id": aid,
        "username": uname,
        "display_name": body.display_name,
        "kind": body.kind,
        "org": node,
        "extra": body.extra,
        "created_at": idgen.now(),
    }
    await db.actors.insert_one(doc)
    return pub(doc)


@router.get("")
async def list_actors(actor: dict = Depends(require_service)):
    return [pub(a) async for a in db.actors.find().sort("username", 1)]


@router.get("/me")
async def me(actor: dict = Depends(require_service)):
    return pub(actor)


@router.get("/tree")
async def tree(actor: dict = Depends(require_service)):
    """Org tree as nested children lists."""
    alla = {a["_id"]: {**pub(a), "children": []} async for a in db.actors.find()}
    roots = []
    for a in alla.values():
        p = a["org"]["parent_id"]
        if p and p in alla:
            alla[p]["children"].append(a)
        else:
            roots.append(a)
    return roots


@router.get("/{actor_id}")
async def get_actor(actor_id: str, actor: dict = Depends(require_service)):
    a = await db.actors.find_one({"_id": actor_id})
    if not a:
        raise HTTPException(404, "no such actor")
    return pub(a)


@router.get("/{actor_id}/superiors")
async def superiors(actor_id: str, actor: dict = Depends(require_service)):
    a = await db.actors.find_one({"_id": actor_id})
    if not a:
        raise HTTPException(404, "no such actor")
    anc = a.get("org", {}).get("ancestors", [])
    out = []
    for s in reversed(anc):  # nearest superior first
        sdoc = await db.actors.find_one({"_id": s})
        if sdoc:
            out.append(pub(sdoc))
    return out


@router.get("/{actor_id}/subtree")
async def subtree(actor_id: str, actor: dict = Depends(require_service)):
    ids = await org.subtree_ids(actor_id)
    out = []
    for i in ids:
        d = await db.actors.find_one({"_id": i})
        if d:
            out.append(pub(d))
    return out


@router.put("/{actor_id}/parent")
async def set_parent(actor_id: str, body: ParentIn,
                     actor: dict = Depends(require_service)):
    target = await db.actors.find_one({"_id": actor_id})
    if not target:
        raise HTTPException(404, "no such actor")
    res = await org.move(actor_id, body.parent_id)
    moved = await db.actors.find_one({"_id": actor_id})
    return {"result": res, "actor": pub(moved)}


@router.delete("/{actor_id}")
async def delete_actor(actor_id: str, actor: dict = Depends(require_service)):
    if actor_id == actor["_id"]:
        raise HTTPException(400, "cannot delete yourself")
    t = await db.actors.find_one({"_id": actor_id})
    if not t:
        raise HTTPException(404, "no such actor")
    kids = await db.actors.count_documents({"org.parent_id": actor_id})
    if kids:
        raise HTTPException(400, "actor has subordinates; move them first")
    await db.conversations.update_many({}, {"$pull": {"members": actor_id}})
    await db.inbox.delete_many({"recipient_id": actor_id})
    await db.actors.delete_one({"_id": actor_id})
    return {"deleted": actor_id}
