import asyncio

from fastapi import FastAPI

from . import db, dispatcher
from .admin import router as admin_router
from .config import env_fallback
from .dispatcher import refresh_catalog
from .jobs import router as jobs_router

app = FastAPI(title="TreeGent proxy", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await db.ensure_indexes()
    await seed_defaults()
    try:
        n = await refresh_catalog()
        ready = sorted((await dispatcher.ready_providers()).keys())
        print(f"catalog refreshed: {n} models from ready providers {ready}")
    except Exception as e:  # noqa: BLE001
        print(f"catalog refresh failed (jobs will fail until providers "
              f"are configured): {e}")
    app.state.worker = asyncio.create_task(dispatcher.worker_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.worker.cancel()


async def seed_defaults() -> None:
    """First-run defaults — provider-AGNOSTIC (R31/R33): classes carry
    criteria (price ceiling / caps / context floor), never model names.
    NO provider is baked into the product: providers are rows an admin
    creates (base_url + key_env) or seeds via TG_PROXY_PROVIDER_BOOTSTRAP_JSON."""
    if await db.task_classes.count_documents({}) == 0:
        await db.task_classes.insert_many([
            {"_id": "cheap", "default": False,
             "description": "fast, low-cost turns (heartbeats, trivial replies)",
             "criteria": {"max_price_out": 1.0, "requires": []}},
            {"_id": "standard", "default": True,
             "description": "normal work turns",
             "criteria": {"max_price_out": 8.0, "requires": []}},
            {"_id": "reasoning", "default": False,
             "description": "hard tasks needing strong reasoning",
             "criteria": {"max_price_out": 25.0, "requires": ["reasoning"]}},
            {"_id": "vision", "default": False,
             "description": "image input required",
             "criteria": {"max_price_out": 25.0, "requires": ["vision"]}},
        ])
    # generic provider bootstrap (any deployment, via env; JSON array of
    # {_id, kind, base_url, key_env} rows) — providers are DATA, never code
    import json
    bootstrap = env_fallback("TG_PROXY_PROVIDER_BOOTSTRAP_JSON")
    if bootstrap:
        try:
            for p in json.loads(bootstrap):
                await db.providers.update_one(
                    {"_id": p["_id"]},
                    {"$set": {**p, "enabled": True}}, upsert=True)
        except Exception as e:  # noqa: BLE001
            print(f"provider bootstrap json invalid: {e}")
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
app.include_router(admin_router)
