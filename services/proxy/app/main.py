import asyncio

from fastapi import FastAPI

from . import db, dispatcher
from .jobs import router as jobs_router

app = FastAPI(title="TreeGent proxy", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await db.ensure_indexes()
    await seed_defaults()
    app.state.worker = asyncio.create_task(dispatcher.worker_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.worker.cancel()


async def seed_defaults() -> None:
    """First-run defaults: one provider (openrouter), task classes, agent key
    for the demo agent. Idempotent."""
    if await db.providers.count_documents({}) == 0:
        await db.providers.insert_one({
            "_id": "openrouter", "kind": "openai-compat",
            "base_url": "https://openrouter.ai/api/v1",
            "enabled": True,
        })
    if await db.task_classes.count_documents({}) == 0:
        await db.task_classes.insert_many([
            {"_id": "cheap", "provider_prefs": ["openrouter"],
             "requires": [], "default": False},
            {"_id": "standard", "provider_prefs": ["openrouter"],
             "requires": [], "default": True},
            {"_id": "reasoning", "provider_prefs": ["openrouter"],
             "requires": ["reasoning"], "default": False},
            {"_id": "vision", "provider_prefs": ["openrouter"],
             "requires": ["vision"], "default": False},
        ])
    # demo agent key (M2 testing; replaced by control enrollment later)
    demo_agent_id = "agt_a9f96a657d2d0962"  # 'worker' actor from M1
    if await db.agent_keys.count_documents({"agent_id": demo_agent_id}) == 0:
        from .db import new_key
        await db.agent_keys.insert_one({
            "_id": new_key(), "agent_id": demo_agent_id, "created_at": dispatcher.now(),
            "revoked": False, "note": "demo"})


@app.get("/health")
async def health():
    await db.client.admin.command("ping")
    return {"ok": True, "service": "proxy"}


app.include_router(jobs_router)
