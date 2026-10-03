"""Mail + approvals API. Auth mirrors chat: X-Service-Token for trusted
services (web/agentd), agent-key auth added with the runtime (M5).

Since R40 every data access goes through the treegent-common permission
engine — rules are rows in `permissions`, deny-by-default."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from treegent_common.perms import can, principal_for, resource_for

from . import adapters
from .config import (actors, approvals, db, mail_messages, mailboxes,
                     settings)

router = APIRouter(tags=["mail"])


async def require_service(x_service_token: str = Header(default="")) -> None:
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")


def now():
    return datetime.now(timezone.utc)


def mpub(m: dict) -> dict:
    return {"id": m["_id"], "mailbox_id": m["mailbox_id"],
            "direction": m["direction"], "from": m.get("from_addr"),
            "to": m.get("to"), "subject": m.get("subject"),
            "text": m.get("text"), "status": m.get("status"),
            "approval_id": m.get("approval_id"), "ts": m.get("ts")}


# ---------------- mailboxes ----------------

class MailboxIn(BaseModel):
    address: str
    kind: str = "personal"          # personal | shared
    owner: str | None = None        # personal: agent actor id
    members: list[str] = []         # shared: agent actor ids


@router.get("/mailboxes", dependencies=[Depends(require_service)])
async def list_mailboxes(caller_id: str = ""):
    """R40 own-scope LISTING: only mailboxes the caller owns or is a member
    of. Superior reach-down never appears here — it happens on a NAMED
    mailbox via /mailboxes/{id}/messages (rule: mailboxes.read)."""
    if not caller_id:
        raise HTTPException(401, "caller_id required")
    p = await principal_for(db, caller_id)
    out = []
    async for mb in mailboxes.find():
        owners = [mb["owner"]] if mb["kind"] == "personal" else []
        members = mb.get("members", []) if mb["kind"] == "shared" else []
        if await can(db, p, "mailboxes.list",
                     await resource_for(db, owners, members)):
            out.append({"id": mb["_id"], "address": mb["address"],
                        "kind": mb["kind"], "owner": mb.get("owner"),
                        "members": members})
    return out


@router.post("/mailboxes", status_code=201,
             dependencies=[Depends(require_service)])
async def create_mailbox(body: MailboxIn):
    if body.kind not in ("personal", "shared"):
        raise HTTPException(400, "kind must be personal or shared")
    if "@" not in body.address:
        raise HTTPException(400, "address must be a full email address")
    if body.kind == "personal" and not body.owner:
        raise HTTPException(400, "personal mailbox needs an owner")
    if await mailboxes.find_one({"address": body.address}):
        raise HTTPException(409, "mailbox address already exists")
    if body.owner:
        a = await actors.find_one({"_id": body.owner, "kind": "agent"})
        if not a:
            raise HTTPException(400, "owner must be an agent actor id")
    for m in body.members:
        a = await actors.find_one({"_id": m, "kind": "agent"})
        if not a:
            raise HTTPException(400, f"member {m!r} must be an agent actor id")
    doc = {"_id": f"mbx_{body.address.replace('@', '_at_')}",
           "address": body.address, "kind": body.kind}
    if body.owner:
        doc["owner"] = body.owner
    if body.members:
        doc["members"] = list(body.members)
    await mailboxes.insert_one(doc)
    return {"id": doc["_id"], "address": body.address}


@router.get("/mailboxes/{mbx_id}/messages",
            dependencies=[Depends(require_service)])
async def list_messages(mbx_id: str, limit: int = 50, caller_id: str = ""):
    mb = await mailboxes.find_one({"_id": mbx_id})
    if not mb:
        raise HTTPException(404, "no such mailbox")
    p = await principal_for(db, caller_id) if caller_id else None
    if p:
        owners = [mb["owner"]] if mb["kind"] == "personal" else []
        members = mb.get("members", []) if mb["kind"] == "shared" else []
        if not await can(db, p, "mailboxes.read",
                         await resource_for(db, owners, members)):
            raise HTTPException(403, "not allowed to read this mailbox")
    out = []
    async for m in mail_messages.find({"mailbox_id": mbx_id}) \
            .sort("ts", -1).limit(min(limit, 200)):
        out.append(mpub(m))
    return out


# ---------------- outbound send (approval-gated) ----------------

class SendIn(BaseModel):
    from_mailbox: str              # mailbox address (personal or shared)
    to: str
    subject: str = ""
    text: str = ""
    html: str = ""
    requester_id: str              # agent actor id (auth in M5 runtime)
    mode: str = "background"       # R39: background | foreground


@router.post("/send", status_code=201,
             dependencies=[Depends(require_service)])
async def send(body: SendIn):
    """R25: agents NEVER send directly — create mail + approval; the
    approver (requester's superior) decides via /approvals."""
    mbx = await mailboxes.find_one({"address": body.from_mailbox})
    if not mbx:
        raise HTTPException(404, f"no mailbox {body.from_mailbox!r}")
    requester = await actors.find_one({"_id": body.requester_id})
    if not requester or requester.get("kind") != "agent":
        raise HTTPException(400, "requester must be an agent actor id")
    # mailbox access: owner or shared member
    if mbx["kind"] == "personal" and mbx.get("owner") != body.requester_id:
        raise HTTPException(403, "not the owner of this mailbox")
    if mbx["kind"] == "shared" and body.requester_id not in mbx.get("members", []):
        raise HTTPException(403, "not a member of this shared mailbox")

    parent_id = (requester.get("org") or {}).get("parent_id")
    superior = await actors.find_one({"_id": parent_id}) if parent_id else None
    if not superior:
        raise HTTPException(400, "requester has no superior in the org tree; "
                                 "nobody could approve this send")

    mail_id = f"mail_{abs(hash((body.requester_id, body.to, body.subject, now().isoformat()))) % 10**16:016d}"
    appr_id = f"apr_{mail_id[5:]}"
    msg = {
        "_id": mail_id, "mailbox_id": mbx["_id"], "direction": "out",
        "from_addr": body.from_mailbox, "to": body.to,
        "subject": body.subject, "text": body.text, "html": body.html or None,
        "status": "pending", "approval_id": appr_id, "ts": now(),
    }
    await mail_messages.insert_one(msg)
    await approvals.insert_one({
        "_id": appr_id,
        "action": "mail.send",
        "payload": {"mail_id": mail_id, "from": body.from_mailbox,
                    "to": body.to, "subject": body.subject},
        "requester_id": body.requester_id,
        "approver_id": superior["_id"],
        "status": "pending",
        "mode": body.mode,   # R39: background | foreground
        "created_at": now(),
    })
    # wake the approver if it's an agent (humans see the web queue)
    if superior.get("kind") == "agent":
        await db.wake_events.insert_one({
            "_id": f"wke_{abs(hash((appr_id, superior['_id']))) % 10**16:016d}",
            "agent_id": superior["_id"], "reason": "approval",
            "approval_id": appr_id, "created_at": now(), "consumed": False})
    return {"mail_id": mail_id, "approval_id": appr_id,
            "status": "pending", "approver": superior["_id"]}


# ---------------- approvals ----------------

class DecideIn(BaseModel):
    decision: str                  # approve | reject
    reason: str = ""


@router.get("/approvals", dependencies=[Depends(require_service)])
async def list_approvals(actor_id: str | None = None, scope: str = "inbox"):
    """R40-scoped: inbox = pending where I'm approver; requested = mine;
    without actor_id = everything I can see (approver/requester/subtree/admin)."""
    if scope == "inbox":
        q = {"approver_id": actor_id, "status": "pending"}
    elif scope == "requested":
        q = {"requester_id": actor_id}
    else:
        q = {}
    out = []
    p = await principal_for(db, actor_id) if actor_id else None
    async for a in approvals.find(q).sort("created_at", 1).limit(200):
        if p:
            owners = [x for x in (a["requester_id"], a["approver_id"]) if x == actor_id]
            if not owners:
                continue  # own-scope: not my approval in any role
        out.append({"id": a["_id"], "action": a["action"],
                    "payload": a["payload"], "status": a["status"],
                    "requester_id": a["requester_id"],
                    "approver_id": a["approver_id"],
                    "created_at": a["created_at"],
                    "decided_at": a.get("decided_at"),
                    "reason": a.get("reason")})
    return out


@router.post("/approvals/{appr_id}/decide",
             dependencies=[Depends(require_service)])
async def decide(appr_id: str, body: DecideIn, actor_id: str = ""):
    """Approver decides. Approve → adapter dispatches the mail for real.
    Reject → reason flows back to the requester as a wake event."""
    a = await approvals.find_one({"_id": appr_id})
    if not a:
        raise HTTPException(404, "no such approval")
    if a["status"] != "pending":
        raise HTTPException(409, f"already {a['status']}")
    if actor_id:
        p = await principal_for(db, actor_id)
        if not await can(db, p, "approvals.decide",
                         {"owners": [a["approver_id"]]}):
            raise HTTPException(403, "not allowed to decide this approval")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")

    if body.decision == "approve":
        await approvals.update_one(
            {"_id": appr_id},
            {"$set": {"status": "approved", "decided_at": now()}})
        mail_id = a["payload"].get("mail_id")
        msg = await mail_messages.find_one({"_id": mail_id})
        if not msg:
            raise HTTPException(500, "mail behind approval vanished")
        try:
            result = await adapters.dispatch_outbound(msg)
            await db.wake_events.insert_one({
                "_id": f"wke_{abs(hash((appr_id, 'approved'))) % 10**16:016d}",
                "agent_id": a["requester_id"], "reason": "approval",
                "approval_id": appr_id,
                "detail": f"approved — mail sent to {msg.get('to')}",
                "created_at": now(), "consumed": False})
            return {"approval_id": appr_id, "status": "approved",
                    "mail": result}
        except Exception as e:  # noqa: BLE001
            await mail_messages.update_one({"_id": mail_id},
                                           {"$set": {"status": "failed"}})
            raise HTTPException(502, f"send failed: {e}") from e
    else:
        await approvals.update_one(
            {"_id": appr_id},
            {"$set": {"status": "rejected", "decided_at": now(),
                      "reason": body.reason}})
        mail_id = a["payload"].get("mail_id")
        if mail_id:
            await mail_messages.update_one({"_id": mail_id},
                                           {"$set": {"status": "failed"}})
        # notify the requesting agent their mail was rejected + why
        await db.wake_events.insert_one({
            "_id": f"wke_{abs(hash((appr_id, 'rejected'))) % 10**16:016d}",
            "agent_id": a["requester_id"], "reason": "approval-rejected",
            "approval_id": appr_id, "detail": body.reason[:500],
            "created_at": now(), "consumed": False})
        return {"approval_id": appr_id, "status": "rejected",
                "reason": body.reason}


# ---------------- inbound simulate (dev) + adapters ----------------

@router.post("/internal/inbound", dependencies=[Depends(require_service)])
async def inbound_hook(address: str, from_addr: str, subject: str,
                       text: str = "", html: str = ""):
    """Dev/test hook: simulate an inbound email (real inbound = Resend
    poll/webhook adapter reading this shape)."""
    return await adapters.ingest_inbound(address, from_addr, subject,
                                         text, html)


@router.get("/adapters", dependencies=[Depends(require_service)])
async def list_adapters():
    return [{"id": r["_id"], "direction": r["direction"], "kind": r["kind"],
             "enabled": r.get("enabled", True)}
            async for r in db.mail_adapters.find()]


@router.put("/adapters/{adapter_id}", status_code=201,
            dependencies=[Depends(require_service)])
async def upsert_adapter(adapter_id: str, direction: str, kind: str,
                         enabled: bool = True):
    if direction not in ("outbound", "inbound"):
        raise HTTPException(400, "direction must be outbound or inbound")
    if kind not in ("sink", "resend"):
        raise HTTPException(400, "kind must be sink or resend")
    await db.mail_adapters.update_one(
        {"_id": adapter_id},
        {"$set": {"direction": direction, "kind": kind,
                  "enabled": enabled}},
        upsert=True)
    return {"id": adapter_id, "direction": direction, "kind": kind,
            "enabled": enabled}
