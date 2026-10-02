import asyncio

from fastapi import FastAPI

from . import db, dispatcher
from .config import env_fallback
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


async def refresh_catalog() -> int:
    """Pull the live model list from every READY provider and upsert the
    catalog (prices per 1M tokens, capabilities, context). Deployments with
    no configured providers keep an empty catalog — jobs then fail with a
    clear 'no model available' instead of calling something unconfigured."""
    import json
    import urllib.request

    n = 0
    ready = await dispatcher.ready_providers()
    for name, p in ready.items():
        if p.get("kind") != "openai-compat":
            continue  # future provider kinds get their own fetchers
        base = p["base_url"].rstrip("/")
        req = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {dispatcher.provider_key(p)}"})
        models = json.load(urllib.request.urlopen(req, timeout=30))["data"]
        for m in models:
            mid = m["id"]
            if ":batch" in mid or ":free" in mid:
                continue
            pr = m.get("pricing") or {}  # OpenRouter-style per-token pricing
            pin = pout = None
            try:
                pin = float(pr.get("prompt", "nan")) * 1_000_000
                pout = float(pr.get("completion", "nan")) * 1_000_000
            except (TypeError, ValueError):
                pass  # plain OpenAI /models has no pricing → default prices
            caps = []
            nm = mid.lower()
            if any(s in nm for s in ("claude", "gpt-4", "gpt-5", "gpt-6",
                                     "gemini", "llama")):
                caps.append("vision")
            if any(s in nm for s in ("sonnet", "gpt-4.1", "gpt-5", "gpt-6",
                                     "gemini-2.5", "o4", "deepseek")):
                caps.append("reasoning")
            await db.model_catalog.update_one(
                {"_id": f"{name}/{mid}"},
                {"$set": {"provider": name, "listed": True, "caps": caps,
                          "ctx": m.get("context_length"),
                          "price_in": pin, "price_out": pout}},
                upsert=True)
            n += 1
    # models of providers that lost their key/config get unlisted
    await db.model_catalog.update_many(
        {"provider": {"$nin": list(ready.keys())}},
        {"$set": {"listed": False}})
    return n


@app.get("/health")
async def health():
    await db.client.admin.command("ping")
    return {"ok": True, "service": "proxy"}


app.include_router(jobs_router)
