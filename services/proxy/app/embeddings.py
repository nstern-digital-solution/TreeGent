"""POST /v1/embeddings (R76): synchronous embedding generation through the
R31b class/modality/rank machinery.

Same auth as /v1/jobs (X-Agent-Key service identity), same metering/usage
rows, same failover-rank behavior. The "embedding" designation routes
through the existing task-class system and the catalog filter additionally
requires the model to SUPPORT embeddings (modality filter pattern).
No embedding model configured -> clean 503 "no embedding model" — the
runtime's derived-memory layer degrades gracefully on it.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, dispatcher
from .jobs import auth_agent

router = APIRouter(prefix="/v1", tags=["embeddings"])

MAX_INPUTS = 256


class EmbedIn(BaseModel):
    input: str | list[str]
    designation: str = "embedding"


@router.post("/embeddings")
async def create_embeddings(body: EmbedIn,
                            agent: dict = Depends(auth_agent)) -> dict:
    inputs = [body.input] if isinstance(body.input, str) else list(body.input)
    if not inputs or len(inputs) > MAX_INPUTS or not all(
            isinstance(t, str) and t for t in inputs):
        raise HTTPException(400, "input must be 1..256 non-empty strings")
    if not await db.task_classes.find_one({"_id": body.designation}):
        raise HTTPException(400, f"unknown designation {body.designation!r}")

    # designation -> class filter, embedding support -> modality filter
    cands = await dispatcher.select_candidates(body.designation, ["embedding"])
    if not cands:
        raise HTTPException(
            503, "no embedding model configured — designate an embeddings "
                 f"model with class {body.designation!r} (callers degrade "
                 "gracefully)")

    job = {"_id": f"job_emb_{secrets.token_hex(8)}",
           "agent_id": agent["agent_id"], "class": body.designation,
           "result": None, "error": None, "reason": "embedding",
           "attempts": 0}
    try:
        resp, model_id = await dispatcher.embed_with_failover(
            inputs, cands, agent_id=agent["agent_id"])
        rows = sorted(resp.get("data") or [],
                      key=lambda d: d.get("index") or 0)
        data = [{"embedding": list(d.get("embedding") or []),
                 "index": d.get("index", i)} for i, d in enumerate(rows)]
        if len(data) != len(inputs) or any(not d["embedding"] for d in data):
            raise dispatcher.ProviderError(
                "provider returned malformed embeddings")
    except dispatcher.ProviderError as e:
        job["error"] = str(e)[:500]
        try:
            await dispatcher.meter(job, job.get("model") or "?", "error", 0.0)
        except Exception as e2:  # noqa: BLE001 — metering never masks the error
            print(f"[proxy] embedding error-meter failed: {type(e2).__name__}",
                  flush=True)
        raise

    usage = resp.get("usage") or {}
    job["result"] = resp
    job["model"] = model_id
    try:
        await dispatcher.meter(job, model_id, "ok", 0.0)
    except Exception as e:  # noqa: BLE001 — metering must not fail the response
        print(f"[proxy] embedding meter failed: {type(e).__name__}", flush=True)
    return {"object": "list",
            "data": data,
            "model": model_id,
            "usage": {"prompt_tokens": usage.get("prompt_tokens") or 0,
                      "total_tokens": usage.get("total_tokens")
                      or usage.get("prompt_tokens") or 0}}
