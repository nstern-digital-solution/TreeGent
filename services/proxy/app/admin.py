"""Admin API for the proxy: providers, classes, model catalog, usage.

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
    key: str = ""       # write-only: stored encrypted, never returned


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
    from .keyvault import encrypt_key
    if body.kind != "openai-compat":
        raise HTTPException(400, f"unsupported kind {body.kind!r} (only "
                                 "openai-compat for now)")
    doc = {"kind": body.kind, "base_url": body.base_url.rstrip("/"),
           "key_env": body.key_env, "enabled": body.enabled}
    if body.key:
        doc["key_enc"] = encrypt_key(body.key)
    await db.providers.update_one({"_id": name}, {"$set": doc}, upsert=True)
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


async def class_names() -> list[str]:
    return [c["_id"] async for c in db.task_classes.find()]


@router.get("/classes")
async def list_classes(_: None = Depends(require_service)):
    return [c async for c in db.task_classes.find()]


@router.put("/classes/{name}", status_code=201)
async def upsert_class(name: str, body: ClassIn,
                       _: None = Depends(require_service)):
    await db.task_classes.update_one(
        {"_id": name},
        {"$set": {"description": body.description}},
        upsert=True)
    return {"id": name}


@router.delete("/classes/{name}")
async def delete_class(name: str, _: None = Depends(require_service)):
    r = await db.task_classes.delete_one({"_id": name})
    if not r.deleted_count:
        raise HTTPException(404, "no such class")
    return {"deleted": name}


# ---------- model catalog (rank / designation / exclusion) ----------

class ModelPolicyIn(BaseModel):
    designations: list[str] | None = None   # e.g. ["agent"] or ["agent","task"]
    rank: int | None = None
    excluded: bool | None = None


@router.get("/catalog")
async def list_catalog(_: None = Depends(require_service)):
    out = []
    async for m in db.model_catalog.find({"listed": True}):
        out.append({"id": m["_id"], "provider": m.get("provider"),
                    "designations": m.get("designations", []),
                    "rank": m.get("rank", 0),
                    "modalities": m.get("modalities", []),
                    "excluded": m.get("excluded", False),
                    "ctx": m.get("ctx")})
    return out


@router.put("/models/{model_id:path}/policy")
async def set_model_policy(model_id: str, body: ModelPolicyIn,
                           _: None = Depends(require_service)):
    """Admin policy on a model: designations, rank, excluded."""
    upd: dict = {}
    if body.designations is not None:
        bad = [d for d in body.designations if d not in await class_names()]
        if bad:
            raise HTTPException(400, f"unknown designations {bad}")
        upd["designations"] = body.designations
    if body.rank is not None:
        upd["rank"] = body.rank
    if body.excluded is not None:
        upd["excluded"] = body.excluded
    if not upd:
        raise HTTPException(400, "nothing to update")
    r = await db.model_catalog.update_one({"_id": model_id}, {"$set": upd})
    if not r.matched_count:
        raise HTTPException(404, "no such model in catalog")
    return {"id": model_id, **upd}


# ---------- usage ----------

@router.get("/usage")
async def usage(days: int = 7, _: None = Depends(require_service)):
    """Per-agent usage aggregation over the last N days (tokens only)."""
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
        }},
    ]
    out = []
    async for row in db.usage_events.aggregate(pipeline):
        out.append({"agent_id": row["_id"], "jobs": row["jobs"],
                    "jobs_ok": row["ok"], "tokens_in": row["tokens_in"],
                    "tokens_out": row["tokens_out"]})
    return out
