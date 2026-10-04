import asyncio

from fastapi import FastAPI

from . import adapters  # noqa: F401  (registers nothing yet, keeps import)
from .config import ensure_indexes, settings
from .main import router


app = FastAPI(title="TreeGent mail", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await ensure_indexes()
    # dev default: capture sink outbound until a Resend key + adapter exist
    from .config import db
    if await db.mail_adapters.count_documents({}) == 0:
        await db.mail_adapters.insert_one(
            {"_id": "outbound-default", "direction": "outbound",
             "kind": "sink", "enabled": True})
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
        # canonical one (covers firstname@ and old-domain renames); if the
        # canonical already exists, drop the stale extra instead.
        async for old_box in db.mailboxes.find(
                {"owner": a["_id"], "kind": "personal", "_id": {"$ne": canonical_id}}):
            if await db.mailboxes.find_one({"_id": canonical_id}):
                await db.mailboxes.delete_one({"_id": old_box["_id"]})
            else:
                await db.mailboxes.update_one(
                    {"_id": old_box["_id"]},
                    {"$set": {"_id": canonical_id, "address": addr}})
        if not await db.mailboxes.find_one({"_id": canonical_id}):
            await db.mailboxes.insert_one({
                "_id": canonical_id, "address": addr, "kind": "personal",
                "owner": a["_id"], "created_at": datetime.datetime.utcnow()})


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "mail"}


app.include_router(router)
