"""Dispatcher: pulls queued jobs by priority, resolves class → model via the
catalog, calls the provider, meters usage, stores history (R34)."""
import asyncio
import time
from datetime import datetime, timezone

import httpx

from . import db
from .config import settings

# rough per-$ per-1k pricing for cost estimates; catalog can override
DEFAULT_PRICES = {"in": 0.15, "out": 0.60}  # per 1M tokens, fallback


def now():
    return datetime.now(timezone.utc)


def _ready_providers() -> set[str]:
    """Providers this deployment can actually call (configured + keyed)."""
    ready = set()
    if settings.openrouter_api_key:
        ready.add("openrouter")
    return ready


async def resolve_model(class_doc: dict) -> tuple[str, str] | None:
    """class → (model_id, provider_name), provider-agnostic (R31/R33).

    Explicit admin override (`models`) wins; otherwise: candidates =
    catalog entries of READY providers matching the class criteria
    (required caps, max output price per 1M tok), cheapest first."""
    # explicit override for determinism when an admin wants it
    requires = (class_doc.get("criteria") or {}).get("requires") or []
    for mid in class_doc.get("models") or []:
        m = await db.model_catalog.find_one({"_id": mid, "listed": True})
        if m and all(c in (m.get("caps") or []) for c in requires):
            prov = mid.split("/", 1)[0]
            if prov in _ready_providers():
                return mid, prov

    crit = class_doc.get("criteria") or {}
    requires = crit.get("requires") or []
    max_out = crit.get("max_price_out")   # USD per 1M completion tokens
    min_ctx = crit.get("min_ctx") or 0
    ready = _ready_providers()
    candidates = []
    async for m in db.model_catalog.find({"listed": True}):
        prov = m["_id"].split("/", 1)[0]
        if prov not in ready:
            continue
        if requires and not all(c in (m.get("caps") or []) for c in requires):
            continue
        if max_out is not None:
            po = m.get("price_out")
            if po is None or po > max_out:
                continue
        if (m.get("ctx") or 0) < min_ctx:
            continue
        candidates.append(m)
    if not candidates:
        return None
    candidates.sort(key=lambda m: (m.get("price_out") or 1e9,
                                   m.get("price_in") or 1e9))
    best = candidates[0]
    return best["_id"], best["_id"].split("/", 1)[0]


async def call_openrouter(model_id: str, job: dict) -> dict:
    """OpenAI-compatible call. Works for any provider with a base_url (R33);
    OpenRouter is the first configured provider. Catalog ids are
    '<provider>/<model>'; the wire needs just the model part."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id.split("/", 1)[1],
        "messages": job["messages"],
        "max_tokens": job.get("max_tokens", 4096),
    }
    if job.get("tools"):
        payload["tools"] = [
            {"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("parameters", {})}}
            for t in job["tools"]
        ]
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"provider {r.status_code}: {r.text[:300]}")
        return r.json()


async def meter(job: dict, model_id: str, provider: str, resp: dict,
                queue_wait_s: float, status: str):
    usage = resp.get("usage") or {}
    tin = usage.get("prompt_tokens") or 0
    tout = usage.get("completion_tokens") or 0
    cat = await db.model_catalog.find_one({"_id": model_id}) or {}
    pin = cat.get("price_in", DEFAULT_PRICES["in"]) / 1_000_000
    pout = cat.get("price_out", DEFAULT_PRICES["out"]) / 1_000_000
    await db.usage_events.insert_one({
        "_id": f"use_{job['_id'][4:]}",
        "job_id": job["_id"],
        "agent_id": job["agent_id"],
        "class": job["class"],
        "model": model_id,
        "provider": provider,
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_total": tin + tout,
        "cost_est": round(tin * pin + tout * pout, 6),
        "queue_wait_s": round(queue_wait_s, 3),
        "status": status,
        "ts": now(),
    })


async def process_job(job: dict):
    cls = await db.task_classes.find_one({"_id": job["class"]})
    resolved = await resolve_model(cls or {})
    if not resolved:
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": "failed", "error": "no model available for class",
            "finished_at": now()}})
        return
    model_id, provider = resolved
    await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
        "status": "dispatched", "dispatched_at": now(), "model": model_id,
        "provider": provider}})
    queue_wait = (time.time() - job["created_at"].timestamp()
                  if job.get("created_at") else 0.0)
    try:
        resp = await call_openrouter(model_id, job)
        await meter(job, model_id, provider, resp, queue_wait, "ok")
        # R34: full history per job
        await db.history.insert_one({
            "_id": job["_id"],
            "agent_id": job["agent_id"],
            "model": model_id,
            "provider": provider,
            "messages": job["messages"],
            "tools": job.get("tools", []),
            "response": resp.get("choices", []),
            "usage": resp.get("usage"),
            "ts": now(),
        })
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": "done", "result": resp, "finished_at": now()}})
    except Exception as e:  # noqa: BLE001
        try:
            await meter(job, model_id, provider, {}, queue_wait, "error")
        except Exception:  # noqa: BLE001
            pass
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": "failed", "error": str(e)[:500], "finished_at": now()}})


async def worker_loop():
    """Single dispatcher loop: always take the highest-priority queued job."""
    while True:
        try:
            job = await db.jobs.find_one_and_update(
                {"status": "queued"},
                {"$set": {"status": "dispatched", "dispatched_at": now()}},
                sort=[("prio", -1), ("created_at", 1)],
            )
            if not job:
                await asyncio.sleep(0.25)
                continue
            # re-mark queued so process_job owns the state machine cleanly
            await db.jobs.update_one({"_id": job["_id"]},
                                     {"$set": {"status": "queued"}})
            await process_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)
