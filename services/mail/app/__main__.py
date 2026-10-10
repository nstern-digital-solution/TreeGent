import asyncio

from fastapi import FastAPI

from . import adapters  # noqa: F401  (registers nothing yet, keeps import)
from .config import ensure_indexes, settings
from .main import router


app = FastAPI(title="TreeGent mail", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await ensure_indexes()
    # R60c: the permission engine is deny-by-default and the rule rows are
    # DATA (R40) — but nothing ever seeded them, so a FRESH deploy denied
    # every mailbox listing (empty list, "no mailbox yet"). Seed idempotently
    # at startup; root can still edit rows later via the permissions UI.
    from treegent_common.perms import RULE_SEEDS
    from .config import db as _db
    for rule in RULE_SEEDS:
        await _db.permissions.update_one(
            {"_id": rule["_id"]},
            {"$setOnInsert": rule}, upsert=True)
    # issue #20 one-time upgrade: rows seeded before root rescue sit at the
    # exact old seed shape ["own"], and $setOnInsert (R60c) never rewrites
    # an existing row — a fresh deploy would pass while an upgraded box
    # silently kept the bug. Additive and shape-gated: only the untouched
    # pre-fix seed shape is upgraded, any root edit is left alone.
    for perm in ("approvals.read", "approvals.decide"):
        await _db.permissions.update_one(
            {"_id": perm, "allow": ["own"]},
            {"$set": {"allow": ["own", "root"]}})
    # dev default: capture sink outbound until a Resend key + adapter exist
    from .config import db
    if await db.mail_adapters.count_documents({}) == 0:
        await db.mail_adapters.insert_one(
            {"_id": "outbound-default", "direction": "outbound",
             "kind": "sink", "enabled": True})
    # issue #13: crash recovery (proxy R68 shape) — approvals stranded in
    # 'dispatching' and requester wakes lost mid-decide must heal without
    # manual surgery. Requeue once at startup, then a 30s sweep loop.
    from . import recovery
    from .config import approvals, wake_events
    await recovery.requeue_stale(approvals, "startup")
    await recovery.heal_lost_wakes(approvals, wake_events, "startup")
    app.state.recovery = asyncio.create_task(
        recovery.recovery_loop(approvals, wake_events))
    # R49/R52: mailboxes = firstname.lastname@<mail domain> (human
    # convention). Domain is deployment data (TG_MAIL_DOMAIN); falls back to
    # username when no persona exists.
    import re
    import datetime
    async for a in db.actors.find({"kind": "agent"}):
        p = a.get("persona") or {}
        if p.get("persona_name"):
            parts = [re.sub(r"[^a-z0-9]", "", x.lower())
                     for x in p["persona_name"].split()]
            parts = [x for x in parts if x][:2] or ["agent"]
            local = ".".join(parts)
        else:
            # no persona (pre-R49 agent): username only, never display-name split
            local = re.sub(r"[^a-z0-9]", "", a.get("username", "").lower()) or "agent"
        addr = f"{local}@{settings.mail_domain}"
        canonical_id = f"mbx_{addr.replace('@', '_at_')}"
        # migrate: any OTHER personal box owned by this agent becomes the
        # canonical one (covers firstname@ and old-domain renames). _id is
        # immutable in Mongo — rewrite = delete + insert, preserving mail.
        if not await db.mailboxes.find_one({"_id": canonical_id}):
            async for old_box in db.mailboxes.find(
                    {"owner": a["_id"], "kind": "personal",
                     "_id": {"$ne": canonical_id}}):
                moved = await db.mail_messages.update_many(
                    {"mailbox_id": old_box["_id"]},
                    {"$set": {"mailbox_id": canonical_id}})
                await db.mailboxes.insert_one({
                    **{k: v for k, v in old_box.items() if k != "_id"},
                    "_id": canonical_id, "address": addr})
                await db.mailboxes.delete_one({"_id": old_box["_id"]})
                print(f"[mail] migrated {old_box['address']} -> {addr} "
                      f"({moved.modified_count} messages)")
        box = await db.mailboxes.find_one({"_id": canonical_id})
        if not box:
            await db.mailboxes.insert_one({
                "_id": canonical_id, "address": addr, "kind": "personal",
                "owner": a["_id"], "created_at": datetime.datetime.utcnow()})
        elif (box.get("kind") == "personal"
              and box.get("owner") != a["_id"]):
            # R60b: the canonical box exists but points at ANOTHER owner.
            # If that owner actor no longer exists, the agent row was
            # recreated with a new id (same persona -> same address ->
            # same _id) and the box is orphaned: claim it for this agent.
            old_owner = await db.actors.find_one({"_id": box.get("owner")})
            if not old_owner:
                await db.mailboxes.update_one(
                    {"_id": canonical_id},
                    {"$set": {"owner": a["_id"]}})
                print(f"[mail] claimed orphaned {addr} for {a['username']} "
                      f"(old owner {box.get('owner')} no longer exists)")
            else:
                # address taken by a LIVE different actor (name collision)
                print(f"[mail] WARN {addr} owned by live actor "
                      f"{box.get('owner')} — not claiming")


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "recovery", None)
    if task:
        task.cancel()


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "mail"}


app.include_router(router)
