"""Dispatcher: pulls queued jobs by priority, selects models by rank (R31b),
calls the provider generically, meters usage, stores history (R34).

Selection (operator ruling): designation filter (class = agent|task) →
modality filter (request needs image/video/audio/file input) → availability
filter (provider/model health cooldowns from live errors) → highest rank.
NO pricing anywhere: rank + exclusions are the admin's policy levers.

Providers are DATA (rows in `providers`: name, kind, base_url, key_env,
enabled) — never code (R33). One generic OpenAI-compatible client serves
every provider of kind 'openai-compat'."""
import asyncio
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .config import env_fallback


def now():
    return datetime.now(timezone.utc)


# ---------------- provider plumbing ----------------

def provider_key(provider_doc: dict) -> str:
    """Resolve a provider's API key. Priority: encrypted key stored on the
    provider row (set via admin UI — write-only, never returned), then
    TG_PROXY_KEY_<NAME> env var, then the env var named by key_env."""
    enc = provider_doc.get("key_enc")
    if enc:
        from .keyvault import decrypt_key
        k = decrypt_key(enc)
        if k:
            return k
    name = (provider_doc.get("_id") or "").upper().replace("-", "_")
    return env_fallback(f"TG_PROXY_KEY_{name}") or env_fallback(
        provider_doc.get("key_env", ""))


async def ready_providers() -> dict[str, dict]:
    """{name: provider_doc} for enabled providers whose key resolves."""
    out: dict[str, dict] = {}
    async for p in db.providers.find({"enabled": True}):
        if provider_key(p):
            out[p["_id"]] = p
    return out


# ---------------- health tracking (availability filter) ----------------

class RateLimited(Exception):
    pass


class ProviderError(Exception):
    pass


async def mark_blocked(scope: str, seconds: int, reason: str) -> None:
    """scope = provider name (whole provider) or 'provider/model'."""
    await db.provider_health.update_one(
        {"_id": scope},
        {"$set": {"blocked_until": now() + timedelta(seconds=seconds),
                  "reason": reason[:200], "ts": now()},
         "$inc": {"fails": 1}},
        upsert=True)


async def mark_ok(provider: str) -> None:
    await db.provider_health.delete_many({"_id": {"$regex": f"^{provider}"}})


async def blocked_scopes() -> set[str]:
    """Scopes currently cooling down (provider- and model-level)."""
    cur = db.provider_health.find({"blocked_until": {"$gt": now()}})
    return {h["_id"] async for h in cur}


# ---------------- model selection (R31b) ----------------

def detect_needs(messages: list[dict]) -> list[str]:
    """Modalities the request actually needs, from its content parts."""
    needs: set[str] = set()
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            t = (part.get("type") or "").lower()
            if t in ("image_url", "image", "input_image"):
                needs.add("image")
            elif t in ("input_audio", "audio_url", "audio"):
                needs.add("audio")
            elif t in ("video_url", "video", "input_video"):
                needs.add("video")
            elif t in ("file_url", "file", "input_file"):
                needs.add("file")
    return sorted(needs)


async def select_candidates(class_name: str, needs: list[str]) -> list[dict]:
    """Ordered candidate models: designation → modality → availability →
    rank (desc). Admin controls rank, designations, excluded flags, and
    provider enabled flags — all data, no code."""
    blocked = await blocked_scopes()
    ready = await ready_providers()
    q: dict = {"listed": True, "excluded": {"$ne": True},
               "designations": class_name}
    if needs:
        q["modalities"] = {"$all": needs}
    cands = []
    async for m in db.model_catalog.find(q):
        prov = m.get("provider")
        bare = m["_id"].split("/", 1)[1]
        if prov not in ready or prov in blocked or f"{prov}:{bare}" in blocked:
            continue
        cands.append(m)
    cands.sort(key=lambda m: -(m.get("rank") or 0))
    return cands


# ---------------- generic provider call + failover ----------------

async def call_provider(provider_doc: dict, model_id: str, job: dict) -> dict:
    """Generic OpenAI-compatible chat completion. model_id is the catalog
    id '<provider>/<model>'; the wire takes the model part only."""
    base = provider_doc["base_url"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {provider_key(provider_doc)}",
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
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{base}/chat/completions", headers=headers,
                             json=payload)
    except httpx.HTTPError as e:
        raise ProviderError(f"connection: {e}") from e
    if r.status_code == 429:
        raise RateLimited(r.headers.get("retry-after", "60"))
    if r.status_code != 200:
        raise ProviderError(f"provider {r.status_code}: {r.text[:200]}")
    return r.json()


async def run_with_failover(job: dict) -> tuple[dict, str]:
    """Walk the ranked candidate list; cooldown-block failures, next model."""
    needs = job.get("needs") or detect_needs(job.get("messages"))
    cands = await select_candidates(job["class"], needs)
    if not cands:
        raise ProviderError("no model available for class "
                            f"{job['class']!r} (needs={needs or 'any'})")
    tried = []
    for m in cands[:4]:
        prov_name = m["provider"]
        provider_doc = (await ready_providers()).get(prov_name)
        if not provider_doc:
            continue
        tried.append(m["_id"])
        try:
            resp = await call_provider(provider_doc, m["_id"], job)
            await mark_ok(prov_name)
            return resp, m["_id"]
        except RateLimited as e:
            wait = int(float(str(e) or 60) or 60)
            await mark_blocked(f"{prov_name}:{m['_id'].split('/', 1)[1]}",
                               min(wait, 900), "429 rate limited")
        except ProviderError as e:
            await mark_blocked(f"{prov_name}:{m['_id'].split('/', 1)[1]}",
                               30, str(e))
    raise ProviderError(f"all candidates failed: {', '.join(tried)}")


# ---------------- metering + history (R34; tokens only, no pricing) ----

async def meter(job: dict, model_id: str, status: str, queue_wait_s: float):
    usage = (job.get("result") or {}).get("usage") or {}
    await db.usage_events.insert_one({
        "_id": f"use_{job['_id'][4:]}",
        "job_id": job["_id"],
        "agent_id": job["agent_id"],
        "class": job["class"],
        "model": model_id,
        "provider": model_id.split("/", 1)[0] if "/" in model_id else None,
        "tokens_in": usage.get("prompt_tokens") or 0,
        "tokens_out": usage.get("completion_tokens") or 0,
        "tokens_total": (usage.get("prompt_tokens") or 0)
                        + (usage.get("completion_tokens") or 0),
        "queue_wait_s": round(queue_wait_s, 3),
        "status": status,
        "ts": now(),
    })


async def process_job(job: dict):
    await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
        "status": "dispatched", "dispatched_at": now()}})
    queue_wait = (time.time() - job["created_at"].timestamp()
                  if job.get("created_at") else 0.0)
    try:
        resp, model_id = await run_with_failover(job)
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "model": model_id, "result": resp}})
        await meter({**job, "result": resp}, model_id, "ok", queue_wait)
        await db.history.insert_one({  # R34: full content history
            "_id": job["_id"],
            "agent_id": job["agent_id"],
            "model": model_id,
            "messages": job["messages"],
            "tools": job.get("tools", []),
            "response": resp.get("choices", []),
            "usage": resp.get("usage"),
            "ts": now(),
        })
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": "done", "finished_at": now()}})
    except Exception as e:  # noqa: BLE001
        await meter(job, job.get("model") or "?", "error", queue_wait)
        await db.jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": "failed", "error": str(e)[:500],
            "finished_at": now()}})


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
            await process_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)


# ---------------- catalog refresh (providers are data) ----------------

async def refresh_catalog() -> int:
    """Pull the live model list from every READY provider. Stores input
    modalities where the provider reports them; prices are NOT stored
    (R31b: rank + exclusions decide, not cost). New models default to
    designations ['task'], rank 0 — an admin promotes models to 'agent'
    and sets ranks in the dashboard."""
    n = 0
    errors: dict[str, str] = {}
    ready = await ready_providers()
    for name, p in ready.items():
        if p.get("kind") != "openai-compat":
            continue  # future provider kinds get their own fetchers
        base = p["base_url"].rstrip("/")
        req = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {provider_key(p)}"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            models = json.load(resp)["data"]
        except urllib.error.HTTPError as e:
            errors[name] = f"HTTP {e.code} from {base}/models"
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
            errors[name] = f"{type(e).__name__}: {e}"[:200]
            continue
        for m in models:
            mid = m["id"]
            if ":batch" in mid or ":free" in mid:
                continue
            arch = m.get("architecture") or {}
            mods = [x for x in (arch.get("input_modalities") or [])
                    if x in ("text", "image", "audio", "video", "file")]
            await db.model_catalog.update_one(
                {"_id": f"{name}/{mid}"},
                {"$set": {"provider": name, "listed": True,
                          "modalities": mods,
                          "ctx": m.get("context_length")},
                 "$setOnInsert": {"designations": ["task"], "rank": 0,
                                  "excluded": False}},
                upsert=True)
            n += 1
    # models of providers that lost their key/config get unlisted
    await db.model_catalog.update_many(
        {"provider": {"$nin": list(ready.keys())}},
        {"$set": {"listed": False}})
    if errors:
        raise ProviderError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    return n
