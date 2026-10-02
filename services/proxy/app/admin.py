"""Admin API for the proxy: classes, providers, catalog, usage, budgets.

Auth: shared service token (Meteor server) — same M1 trusted-service model
(X-Service-Token header). Keys are never returned; only key_env names and
a ready/not-ready flag per provider."""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import db, dispatcher
from .security import require_service

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- providers ----------

class ProviderIn(BaseModel):
    kind: str = "openai-compat"
    base_url: str
    key_env: str = ""
    enabled: bool = True


@router.get("/providers")
async def list_providers(_: None = Depends(require_service)):
    ready = await dispatcher.ready_providers()
    out = []
    async for p in db.providers.find():
        out.append({
            "id": p["_id"], "kind": p.get("kind", "openai-compat"),
            "base_url": p.get("base_url"), "key_env": p.get("key_env", ""),
            "enabled": p.get("enabled", True),
            "ready": p["_id"] in ready,
        })
    return out


@router.put("/providers/{name}", status_code=201)
async def upsert_provider(name: str, body: ProviderIn,
                          _: None = Depends(require_service)):
    if body.kind != "openai-compat":
        raise HTTPException(400, f"unsupported kind {body.kind!r} (only "
                                 "openai-compat for now)")
    await db.providers.update_one(
        {"_id": name},
        {"$set": {"kind": body.kind, "base_url": body.base_url.rstrip("/"),
                  "key_env": body.key_env, "enabled": body.enabled}},
        upsert=True)
    return {"id": name, "ready": name in await dispatcher.ready_providers()}


@router.delete("/providers/{name}")
async def delete_provider(name: str, _: None = Depends(require_service)):
    r = await db.providers.delete_one({"_id": name})
    if not r.deleted_count:
        raise HTTPException(404, "no such provider")
    await db.model_catalog.update_many({"provider": name},
                                       {"$set": {"listed": False}})
    return {"deleted": name}


@router.post("/providers/refresh-catalog")
async def refresh_catalog_ep(_: None = Depends(require_service)):
    n = await dispatcher.refresh_catalog()
    return {"catalog_models": n,
            "ready_providers": sorted((await dispatcher.ready_providers()).keys())}


# ---------- task classes ----------

class ClassIn(BaseModel):
    description: str = ""
    criteria: dict
    models: list[str] | None = None  # explicit admin override
    default: bool = False


@router.get("/classes")
async def list_classes(_: None = Depends(require_service)):
    return [c async for c in db.task_classes.find()]


@router.put("/classes/{name}", status_code=201)
async def upsert_class(name: str, body: ClassIn,
                       _: None = Depends(require_service)):
    crit = body.criteria or {}
    if "max_price_out" in crit and not isinstance(crit["max_price_out"], (int, float)):
        raise HTTPException(400, "max_price_out must be a number (USD per 1M tokens)")
    if body.default:
        await db.task_classes.update_many({}, {"$set": {"default": False}})
    await db.task_classes.update_one(
        {"_id": name},
        {"$set": {"description": body.description, "criteria": crit,
                  "models": body.models, "default": body.default}},
        upsert=True)
    return {"id": name}


@router.delete("/classes/{name}")
async def delete_class(name: str, _: None = Depends(require_service)):
    if name == "standard":
        raise HTTPException(400, "cannot delete the default class")
    r = await db.task_classes.delete_one({"_id": name})
    if not r.deleted_count:
        raise HTTPException(404, "no such class")
    return {"deleted": name}


# ---------- catalog ----------

@router.get("/catalog")
async def list_catalog(only_listed: bool = True,
                       _: None = Depends(require_service)):
    q = {"listed": True} if only_listed else {}
    out = []
    async for m in db.model_catalog.find(q).sort("price_out", 1):
        out.append({"id": m["_id"], "provider": m.get("provider"),
                    "caps": m.get("caps", []), "ctx": m.get("ctx"),
                    "price_in": m.get("price_in"), "price_out": m.get("price_out"),
                    "listed": m.get("listed", True)})
    return out


# ---------- usage / budgets ----------

@router.get("/usage")
async def usage(days: int = 7, _: None = Depends(require_service)):
    """Per-agent usage aggregation over the last N days."""
    import time
    from datetime import datetime, timezone
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc)
    pipeline = [
        {"$match": {"ts": {"$gte": cutoff}}},
        {"$group": {
            "_id": "$agent_id",
            "jobs": {"$sum": 1},
            "ok": {"$sum": {"$cond": [{"$eq": ["$status", "ok"]}, 1, 0]}},
            "tokens_in": {"$sum": "$tokens_in"},
            "tokens_out": {"$sum": "$tokens_out"},
            "cost": {"$sum": "$cost_est"},
        }},
        {"$sort": {"cost": -1}},
    ]
    out = []
    async for row in db.usage_events.aggregate(pipeline):
        out.append({"agent_id": row["_id"], "jobs": row["jobs"],
                    "jobs_ok": row["ok"], "tokens_in": row["tokens_in"],
                    "tokens_out": row["tokens_out"],
                    "cost_usd": round(row["cost"], 6)})
    return out


class BudgetIn(BaseModel):
    monthly_usd: float | None = None


@router.put("/budgets/{agent_id}")
async def set_budget(agent_id: str, body: BudgetIn,
                     _: None = Depends(require_service)):
    await db.agent_keys.update_many(
        {"agent_id": agent_id},
        {"$set": {"monthly_budget_usd": body.monthly_usd}})
    return {"agent_id": agent_id, "monthly_usd": body.monthly_usd}
