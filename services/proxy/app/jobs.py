"""Job submission, priority (R32), serialization, class routing (R31)."""
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from . import db
from .dispatcher import stale_cutoff

router = APIRouter(prefix="/v1", tags=["jobs"])

STATUSES = ("queued", "dispatched", "done", "failed")


class ToolDef(BaseModel):
    name: str
    description: str = ""
    parameters: dict = Field(default_factory=dict)


class JobIn(BaseModel):
    class_name: str = "agent"
    messages: list[dict]
    tools: list[ToolDef] = Field(default_factory=list)
    max_tokens: int = 4096
    reason: str = ""  # heartbeat | dm | mention | admin | background


def now() -> datetime:
    return datetime.now(timezone.utc)


async def auth_agent(x_agent_key: str = Header(default="")) -> dict:
    if not x_agent_key:
        raise HTTPException(401, "X-Agent-Key required")
    k = await db.agent_keys.find_one({"_id": x_agent_key})
    if not k or k.get("revoked"):
        raise HTTPException(401, "bad agent key")
    # R69 fix: a deleted agent's key kept authorizing PAID generations —
    # delete_actor orphans key rows; verify the actor too
    a = await db.actors.find_one({"_id": k["agent_id"]}, {"_id": 1})
    if not a:
        raise HTTPException(401, "agent no longer exists")
    return {"agent_id": k["agent_id"]}


async def priority(agent_id: str, reason: str) -> float:
    """R32: org-hierarchy weight × inverse usage (trailing 48h).

    depth 0 (top) → weight 1.0; each level down halves the weight.
    usage factor: 1.0 at 0 tokens over 48h, decaying toward 0.05 as usage
    grows (half-life = 200k tokens). Heartbeat/background reasons get a
    fixed 0.5 multiplier so interactive work wins ties.
    """
    import httpx
    from .config import settings

    depth = 5  # default weight if chat unreachable: mid-level
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.chat_url}/internal/org-weights",
                            headers={"X-Service-Token": settings.service_token})
            if r.status_code == 200:
                depth = r.json().get(agent_id, 5)
    except Exception:
        pass
    hierarchy = 1.0 / (2 ** max(depth, 0))

    # R69 fix: usage rows store datetime ts — a float-epoch cutoff matched
    # ZERO rows (type bracketing), so heavy agents were never deprioritized
    from datetime import datetime, timedelta, timezone as _tz
    cutoff = datetime.now(_tz.utc) - timedelta(hours=48)
    pipeline = [
        {"$match": {"agent_id": agent_id, "ts": {"$gt": cutoff},
                    "tokens_total": {"$exists": True}}},
        {"$group": {"_id": None, "tok": {"$sum": "$tokens_total"}}},
    ]
    used = 0
    async for row in db.usage_events.aggregate(pipeline):
        used = row["tok"]
    usage = 0.05 + 0.95 * (0.5 ** (used / 200_000))

    reason_mult = 0.5 if reason in ("heartbeat", "background") else 1.0
    return hierarchy * usage * reason_mult


@router.post("/jobs", status_code=201)
async def submit_job(body: JobIn, agent: dict = Depends(auth_agent)) -> dict:
    aid = agent["agent_id"]

    # R32 serialization: reject a second in-flight job per agent.
    # R68: only RECENT dispatched rows count as in flight — a stale one is
    # an orphan from a crash (age-tolerant; the sweep/startup requeues it)
    # and must not lock the agent out of new submissions forever.
    inflight = await db.jobs.count_documents({
        "agent_id": aid,
        "$or": [{"status": "queued"},
                {"status": "dispatched", "dispatched_at": {"$gte": stale_cutoff()}}]})
    if inflight:
        raise HTTPException(409, "agent already has an in-flight generation "
                                 "(R32: one at a time; queue at the turn level)")

    if not await db.task_classes.find_one({"_id": body.class_name}):
        raise HTTPException(400, f"unknown class {body.class_name!r}")

    prio = await priority(aid, body.reason)
    doc = {
        "_id": f"job_{secrets.token_hex(8)}",
        "agent_id": aid,
        "class": body.class_name,
        "messages": body.messages,
        "tools": [t.model_dump() for t in body.tools],
        "max_tokens": body.max_tokens,
        "reason": body.reason,
        "status": "queued",
        "prio": prio,
        "model": None,          # R57: filled at dispatch time
        "tokens_in": None,      # R57: filled at completion
        "tokens_out": None,
        "created_at": now(),
        "dispatched_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    await db.jobs.insert_one(doc)
    return {"job_id": doc["_id"], "status": "queued", "priority": prio}


@router.get("/history")
async def job_history(limit: int = 100, agent: dict = Depends(auth_agent)):
    """R57: own inference history — model, tokens, status, error per job."""
    out = []
    async for j in db.jobs.find(
            {"agent_id": agent["agent_id"]},
            {"messages": 0, "tools": 0}).sort("created_at", -1).limit(limit):
        out.append({
            "job_id": j["_id"], "created_at": j.get("created_at"),
            "model": j.get("model"), "status": j.get("status"),
            "tokens_in": j.get("tokens_in"), "tokens_out": j.get("tokens_out"),
            "error": j.get("error"), "reason": j.get("reason"),
        })
    return {"jobs": out}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, agent: dict = Depends(auth_agent)):
    j = await db.jobs.find_one({"_id": job_id})
    if not j or j["agent_id"] != agent["agent_id"]:
        raise HTTPException(404, "no such job")
    return {
        "job_id": j["_id"],
        "status": j["status"],
        "result": j.get("result"),
        "error": j.get("error"),
        "created_at": j.get("created_at"),
        "finished_at": j.get("finished_at"),
    }
