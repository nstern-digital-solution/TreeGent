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
    # R49: mailboxes derived from the agent's persona first name
    # (ada@domain); falls back to username when no persona exists
    import re
    import datetime
    async for a in db.actors.find({"kind": "agent"}):
        p = a.get("persona") or {}
        first = (p.get("persona_name") or a.get("display_name")
                 or a.get("username") or "agent")
        first = first.split()[0].lower()
        first = re.sub(r"[^a-z0-9]", "", first) or "agent"
        addr = f"{first}@{settings.mail_domain}"
        if not await db.mailboxes.find_one({"address": addr}):
            await db.mailboxes.insert_one({
                "_id": f"mbx_{addr.replace('@', '_at_')}",
                "address": addr, "kind": "personal", "owner": a["_id"],
                "created_at": datetime.datetime.utcnow()})


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "mail"}


app.include_router(router)
