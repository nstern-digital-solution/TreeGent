import asyncio

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
    # R81: humans need a username; agents may pass "" (generated name)
    # or an agent-N infra handle (moved to infra_id, name generated).
    username: str = Field(default="", max_length=32,
                          pattern=r"^$|^[a-z0-9_.-]{2,32}$")
    display_name: str = Field(min_length=1, max_length=64)
    kind: str = "human"
    parent_id: str | None = None
    host_id: str | None = None   # R53: agent host assignment
    # R81: operator-set infra handle ("agent-1") for VPS mapping; NOT the
    # username (which is the generated name people address agents by).
    infra_id: str | None = None
    extra: dict = Field(default_factory=dict)


class ParentIn(BaseModel):
    parent_id: str | None = None


_INFRA_RE = None  # compiled lazily


def _is_infra_handle(u: str | None) -> bool:
    r"""R81: operator-style handle like 'agent-1', 'agent-32' — matches
    ^agent-\d+$ (case-insensitive). These become infra_id, not username."""
    import re
    return bool(u) and bool(re.fullmatch(r"agent-\d+", u.strip().lower()))


async def _persona_username(persona: dict) -> str:
    """R81: username = persona's given name, lowercased. Uniqueness by
    suffixing (mila2, mila3) — never re-draw the persona; the name people
    address must be the name the agent calls itself."""
    import re as _re
    first = (persona.get("persona_name") or "agent").split()[0].lower()
    base = _re.sub(r"[^a-z0-9]", "", first) or "agent"
    if not await db.actors.find_one({"username": base}):
        return base
    n = 2
    while await db.actors.find_one({"username": f"{base}{n}"}):
        n += 1
    return f"{base}{n}"


def pub(a: dict) -> dict:
    return {
        "id": a["_id"],
        "username": a["username"],
        "display_name": a["display_name"],
        "kind": a["kind"],
        "org": a.get("org", {"parent_id": None, "ancestors": [], "depth": 0}),
        "infra_id": a.get("infra_id"),          # R81: operator VPS handle
        "created_at": a.get("created_at"),
        "extra": a.get("extra", {}),
    }


@router.post("", status_code=201)
async def create_actor(body: ActorIn, actor: dict = Depends(ORG_ADMIN_BOOTSTRAP)):
    if body.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    uname = body.username.lower()
    # R81: the agent's USERNAME is its generated given name — what people
    # @mention and what appears in forge issues/PRs. An operator-supplied
    # "agent-N" handle is an infra label: moved to infra_id (VPS mapping),
    # never used as the username.
    persona = None
    if body.kind == "agent":
        if _is_infra_handle(body.username):
            body.infra_id = body.infra_id or body.username
        from treegent_common.identity import assign_identity
        persona = assign_identity()
        if _is_infra_handle(body.username) or not body.username:
            uname = await _persona_username(persona)
    # R15 (review): the regex permits a literal ".." — join(root, username)
    # then escapes the per-agent workspace root. Reject any username whose
    # path segments are not plain.
    if uname in (".", "..") or "/".strip() in uname or any(
            s in (".", "..") for s in uname.split("/")) or uname.startswith("."):
        raise HTTPException(400, "username must not contain path segments")
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
        # R49+R81: one persona draw per agent. Username = its given name,
        # display_name = full persona name, infra_id = operator handle.
        doc["persona"] = persona
        doc["display_name"] = persona["persona_name"]
        if body.infra_id:
            doc["infra_id"] = body.infra_id
        if body.host_id:
            doc["host_id"] = body.host_id   # R53: claimed by that host's runtime
    await db.actors.insert_one(doc)
    # R80: forge account, fire-and-forget — never blocks or fails creation.
    # R11 (review): pass the FULL internal document — pub() renames _id to
    # id, and forge.provision reads actor["_id"], so the pub() shape raised
    # KeyError inside the totality wrapper: zero API calls, no pending
    # state, silent no-op. The internal doc is the contract.
    from .. import forge
    asyncio.ensure_future(forge.provision(doc))
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


class RenameIn(BaseModel):
    # R81: humans need a username; agents may pass "" (generated name)
    # or an agent-N infra handle (moved to infra_id, name generated).
    username: str = Field(default="", max_length=32,
                          pattern=r"^$|^[a-z0-9_.-]{2,32}$")


@router.put("/{actor_id}/username")
async def rename_actor(body: RenameIn, actor_id: str,
                       actor: dict = Depends(ORG_ADMIN)):
    """R81: rename an agent — the generated name is the addressable
    username. Propagates: actor doc, forge account (admin API rename,
    old handle 307-redirects so existing issue/PR refs keep resolving),
    and pins the old workspace via symlink (runtime keeps resolving the
    actor's ORIGINAL workspace path until restart re-reads username)."""
    import re as _re
    new = body.username.lower()
    if _is_infra_handle(new):
        raise HTTPException(400, "agent-N style names are infra handles "
                                 "(infra_id), not usernames")
    if new in (".", "..") or any(s in (".", "..") for s in new.split("/")) \
            or new.startswith("."):
        raise HTTPException(400, "username must not contain path segments")
    if await db.actors.find_one({"username": new}):
        raise HTTPException(409, "username taken")
    tgt = await db.actors.find_one({"_id": actor_id})
    if not tgt:
        raise HTTPException(404, "no such actor")
    old_uname = tgt["username"]

    await db.actors.update_one(
        {"_id": actor_id},
        {"$set": {"username": new,
                  "renamed_from": old_uname, "renamed_at": idgen.now()}})

    # forge rename (best-effort; recorded on the doc if it fails so
    # sync_pending-style retry can pick it up)
    from .. import forge
    try:
        ok = await forge.rename_account(old_uname, new)
        await db.actors.update_one(
            {"_id": actor_id},
            {"$set": {"forge.username": new,
                      **({"forge.rename_status": "ok"} if ok else
                         {"forge.rename_status": "pending",
                          "forge.error": "rename not confirmed"})}})
    except Exception as e:  # noqa: BLE001 — rename must not fail the API
        await db.actors.update_one(
            {"_id": actor_id},
            {"$set": {"forge.rename_status": "pending",
                      "forge.error": f"rename: {str(e)[:200]}"}})

    # workspace pinning: chat runs central; runtime hosts own workspaces.
    # We cannot mkdir on agent hosts from here — the runtime's Agent init
    # already re-derives workspace from username and SOUL files carry over
    # because the directory is renamed by the runtime host manager.
    return {"ok": True, "username": new, "renamed_from": old_uname}
