from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, idgen, org
from ..security import require_org_admin, require_service

router = APIRouter(prefix="/actors", tags=["actors"])

# R62 sec-01 org-authorization model: org mutations (create/reparent/delete)
# run ONLY as the root human on the central tier (or the first-actor
# bootstrap pseudo-actor for the very first create). Reads stay dual-tier.
ORG_ADMIN = require_org_admin()
ORG_ADMIN_BOOTSTRAP = require_org_admin(allow_bootstrap=True)

KINDS = ("human", "agent")


class ActorIn(BaseModel):
    # R69: usernames become filesystem workspaces (<root>/<username>) —
    # constrain to a path-safe charset (../ escapes were possible)
    username: str = Field(min_length=2, max_length=32,
                          pattern=r"^[a-z0-9_.-]{2,32}$")
    display_name: str = Field(min_length=1, max_length=64)
    kind: str = "human"
    parent_id: str | None = None
    host_id: str | None = None   # R53: agent host assignment
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
async def create_actor(body: ActorIn, actor: dict = Depends(ORG_ADMIN_BOOTSTRAP)):
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
    if body.kind == "agent":
        # R49: random human name + personality, assigned at creation; used
        # by the runtime system prompt and the agent's mail address.
        from treegent_common.identity import assign_identity
        doc["persona"] = assign_identity()
        doc["display_name"] = doc["persona"]["persona_name"]
        if body.host_id:
            doc["host_id"] = body.host_id   # R53: claimed by that host's runtime
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
                     actor: dict = Depends(ORG_ADMIN)):
    target = await db.actors.find_one({"_id": actor_id})
    if not target:
        raise HTTPException(404, "no such actor")
    res = await org.move(actor_id, body.parent_id)
    moved = await db.actors.find_one({"_id": actor_id})
    return {"result": res, "actor": pub(moved)}


@router.delete("/{actor_id}")
async def delete_actor(actor_id: str, actor: dict = Depends(ORG_ADMIN)):
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
    # R69: orphaned keys kept authorizing paid proxy generations for a
    # deleted agent, and orphaned wakes polled forever
    await db.agent_keys.update_many({"agent_id": actor_id},
                                    {"$set": {"revoked": True}})
    await db.wake_events.delete_many({"agent_id": actor_id})
    # issue #20: the mail service's `approvals` name actors in BOTH roles
    # (requester_id + approver_id) — deleting the actor orphaned rows that
    # nobody could see or decide and left its mail pending forever. Remove
    # every row naming it (root rescue covers pre-fix orphans), settle any
    # still-pending mail behind a removed approval (it can never be decided
    # again) and drop the wakes pointing at the removed approvals.
    removed, dead_mail = [], []
    async for ap in db.approvals.find(
            {"$or": [{"requester_id": actor_id}, {"approver_id": actor_id}]},
            {"_id": 1, "payload.mail_id": 1}):
        removed.append(ap["_id"])
        mid = (ap.get("payload") or {}).get("mail_id")
        if mid:
            dead_mail.append(mid)
    if removed:
        await db.approvals.delete_many({"_id": {"$in": removed}})
        await db.wake_events.delete_many({"approval_id": {"$in": removed}})
    if dead_mail:
        await db.mail_messages.update_many(
            {"_id": {"$in": dead_mail}, "status": "pending"},
            {"$set": {"status": "failed"}})
    # same gap for the mailboxes themselves: a personal box dies with its
    # owner (its mail goes with it); shared boxes keep existing with the
    # membership pulled — mirrors the conversations handling above.
    async for mb in db.mailboxes.find(
            {"$or": [{"owner": actor_id}, {"members": actor_id}]},
            {"_id": 1, "kind": 1, "owner": 1}):
        if mb.get("kind") == "personal" and mb.get("owner") == actor_id:
            await db.mail_messages.delete_many({"mailbox_id": mb["_id"]})
            await db.mailboxes.delete_one({"_id": mb["_id"]})
        else:
            await db.mailboxes.update_one({"_id": mb["_id"]},
                                          {"$pull": {"members": actor_id}})
    await db.actors.delete_one({"_id": actor_id})
    return {"deleted": actor_id}
