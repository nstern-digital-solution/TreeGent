import asyncio

from fastapi import FastAPI

from . import db, dispatcher
from .config import settings
from .dispatcher import _ready_providers
from .jobs import router as jobs_router

app = FastAPI(title="TreeGent proxy", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await db.ensure_indexes()
    await seed_defaults()
    try:
        n = await refresh_catalog()
        print(f"catalog refreshed: {n} models from ready providers "
              f"{sorted(_ready_providers())}")
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
    Which models exist is a property of THIS deployment's configured
    providers + their live catalogs (see refresh_catalog)."""
    if await db.providers.count_documents({}) == 0:
        # 'openrouter' is only registered as a provider when a key exists;
        # deployments without it simply have an empty catalog until they
        # configure providers via admin/env.
        if settings.openrouter_api_key:
            await db.providers.insert_one({
                "_id": "openrouter", "kind": "openai-compat",
                "base_url": "https://openrouter.ai/api/v1",
                "enabled": True,
            })
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
    # demo agent key (M2 testing; replaced by control enrollment later)
    demo_agent_id = "agt_a9f96a657d2d0962"  # 'worker' actor from M1
    if await db.agent_keys.count_documents({"agent_id": demo_agent_id}) == 0:
        from .db import new_key
        await db.agent_keys.insert_one({
            "_id": new_key(), "agent_id": demo_agent_id, "created_at": dispatcher.now(),
            "revoked": False, "note": "demo"})


async def refresh_catalog() -> int:
    """Pull the live model list from every READY provider and upsert the
    catalog (prices per 1M tokens, capabilities, context). Deployments
    with no keyed providers keep an empty catalog — jobs then fail with a
    clear 'no model available' instead of calling something unconfigured."""
    import urllib.request

    n = 0
    if settings.openrouter_api_key:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"})
        import json
        models = json.load(urllib.request.urlopen(req, timeout=30))["data"]
        for m in models:
            mid = m["id"]
            if ":batch" in mid or ":free" in mid:
                continue
            p = m.get("pricing") or {}

            def f(x):
                try:
                    return float(x) * 1_000_000
                except (TypeError, ValueError):
                    return None

            caps = []
            name = mid.lower()
            if any(s in name for s in ("claude", "gpt-4", "gemini", "llama")):
                caps.append("vision")
            if any(s in name for s in ("sonnet", "gpt-4.1", "gemini-2.5", "o4", "deepseek")):
                caps.append("reasoning")
            await db.model_catalog.update_one(
                {"_id": f"openrouter/{mid}"},
                {"$set": {"provider": "openrouter", "listed": True, "caps": caps,
                          "ctx": m.get("context_length"),
                          "price_in": f(p.get("prompt")),
                          "price_out": f(p.get("completion"))}},
                upsert=True)
            n += 1
    # anything in the catalog whose provider is gone/not ready stays but is
    # unlisted so resolution skips it
    await db.model_catalog.update_many(
        {"provider": {"$nin": list(_ready_providers())}},
        {"$set": {"listed": False}})
    return n


@app.get("/health")
async def health():
    await db.client.admin.command("ping")
    return {"ok": True, "service": "proxy"}


app.include_router(jobs_router)
