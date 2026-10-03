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
    # ensure mailboxes exist for every agent actor (personal, dev domain)
    async for a in db.actors.find({"kind": "agent"}):
        addr = f"{a['username']}@{settings.mail_domain}"
        if not await db.mailboxes.find_one({"address": addr}):
            await db.mailboxes.insert_one({
                "_id": f"mbx_{addr.replace('@', '_at_')}",
                "address": addr, "kind": "personal", "owner": a["_id"]})


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "mail"}


app.include_router(router)
