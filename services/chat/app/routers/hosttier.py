"""R56 host-tier endpoints (chat).

The hosted runtime no longer holds Mongo credentials. It authenticates
with its per-host key (X-Host-Id + X-Host-Key, checked against a hash in
the agent_hosts row) and everything it can see is scoped to the agents
assigned to THAT host:

    GET  /internal/host/agents            -> own agents + their key values
    GET  /internal/host/pending/<aid>     -> undelivered inbox + unconsumed
                                             wakes for OWN agent only
    POST /internal/host/turn/<aid>        -> record a turn summary (viewer)
    POST /internal/host/delivered/<aid>   -> mark ids delivered/consumed

Central runtime keeps its direct-Mongo path (it runs on the shared box,
exec off). Hosts NEVER get another host's or agent's data.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import db, idgen
from treegent_common.hostauth import verify_host_key

router = APIRouter(prefix="/internal/host", tags=["host-tier"])


async def _host(
    x_host_id: str = Header(default=""),
    x_host_key: str = Header(default=""),
) -> dict:
    """Resolve + authenticate the calling host; returns its row."""
    if not x_host_id or not x_host_key:
        raise HTTPException(401, "host credentials required")
    h = await db.db.agent_hosts.find_one({"_id": x_host_id})
    if not h or not verify_host_key(x_host_key, h.get("host_key_hash")):
        raise HTTPException(401, "bad host credentials")
    return h


async def _own_agent(host: dict, agent_id: str) -> dict:
    """404 unless the agent is assigned to this exact host."""
    a = await db.actors.find_one({"_id": agent_id, "kind": "agent"})
    if not a or (a.get("host_id") or None) != host["_id"]:
        raise HTTPException(404, "no such agent on this host")
    return a


@router.get("/agents")
async def host_agents(host: dict = Depends(_host)):
    """Own agents with their CURRENT key values (rotated keys are picked
    up on the next poll — no restart needed)."""
    out = []
    async for a in db.actors.find({"kind": "agent", "host_id": host["_id"]}):
        kd = await db.agent_keys.find_one(
            {"agent_id": a["_id"], "revoked": {"$ne": True}})
        out.append({
            "_id": a["_id"],
            "username": a.get("username"),
            "display_name": a.get("display_name") or a.get("username"),
            "persona": a.get("persona"),
            "org": a.get("org"),
            "key": kd["_id"] if kd else None,   # absent => host skips agent
        })
    return {"agents": out, "now": idgen.now()}


@router.get("/pending/{agent_id}")
async def host_pending(agent_id: str, host: dict = Depends(_host)):
    """Undelivered inbox rows + unconsumed wakes for ONE own agent."""
    await _own_agent(host, agent_id)
    inbox = []
    async for i in db.inbox.find(
            {"recipient_id": agent_id, "delivered_at": None},
            sort=[("received_at", 1)], limit=10):
        m = await db.messages.find_one({"_id": i["message_id"]})
        if not m:
            continue
        s = await db.actors.find_one({"_id": m["sender_id"]},
                                     {"username": 1})
        inbox.append({
            "inbox_id": i["_id"],
            "body": m.get("body", ""),
            "sender_username": m.get("sender_username")
                                or (s or {}).get("username", "?"),
            "conversation_id": m["conversation_id"],
            "ts": i.get("received_at"),
        })
    wakes = []
    async for w in db.wake_events.find(
            {"agent_id": agent_id, "consumed": False},
            sort=[("created_at", 1)], limit=10):
        wakes.append({"wake_id": w["_id"], "reason": w.get("reason"),
                      "ts": w.get("created_at")})
    pending_mail = await db.db.mail_messages.count_documents(
        {"mailbox_owner": agent_id, "injected_at": None})
    return {"inbox": inbox, "wakes": wakes, "mail_pending": pending_mail}


@router.post("/delivered/{agent_id}")
async def host_delivered(agent_id: str, body: dict, host: dict = Depends(_host)):
    """Mark EXACTLY the ids the host rendered this turn (id-scoped —
    anything that arrived mid-turn stays pending; fixes silent loss)."""
    await _own_agent(host, agent_id)
    inbox_ids = body.get("inbox_ids") or []
    wake_ids = body.get("wake_ids") or []
    if inbox_ids:
        await db.inbox.update_many(
            {"_id": {"$in": inbox_ids}, "recipient_id": agent_id},
            {"$set": {"delivered_at": idgen.now()}})
    if wake_ids:
        await db.wake_events.update_many(
            {"_id": {"$in": wake_ids}, "agent_id": agent_id},
            {"$set": {"consumed": True}})
    return {"ok": True}


class TurnReport(BaseModel):
    turn_id: str
    trigger: str
    steps: int = 0
    final: str = ""
    started: str = ""
    ended: str = ""


@router.post("/turn/{agent_id}")
async def host_turn(agent_id: str, body: TurnReport, host: dict = Depends(_host)):
    """Turn summary for the admin viewer (full transcript stays on the
    host in the agent workspace; only the summary travels)."""
    await _own_agent(host, agent_id)
    await db.db.runtime_turns.update_one(
        {"_id": body.turn_id},
        {"$set": {
            "agent_id": agent_id, "host_id": host["_id"],
            "trigger": body.trigger, "steps": body.steps,
            "final": body.final[:4000],
            "started": body.started, "ended": body.ended,
        }}, upsert=True)
    return {"ok": True}
