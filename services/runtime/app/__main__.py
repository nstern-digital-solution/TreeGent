from fastapi import FastAPI, Header, HTTPException

from .config import client, db, settings
from .loop import supervise

app = FastAPI(title="TreeGent runtime", version="0.1.0")

# live registry the supervisor refreshes (for the /internal endpoint)
AGENTS_STATE: dict = {}


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    app.state.supervisor = asyncio.create_task(supervise(AGENTS_STATE))


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.supervisor.cancel()


@app.get("/health")
async def health():
    await client.admin.command("ping")
    return {"ok": True, "service": "runtime"}


@app.get("/internal/agents")
async def agents_overview(x_service_token: str = Header(default="")):
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")
    return {"agents": [{"id": aid, "busy": st.get("busy", False),
                        "name": st.get("name")}
                       for aid, st in AGENTS_STATE.items()]}


@app.get("/internal/history/{agent_id}")
async def agent_history(agent_id: str, x_service_token: str = Header(default=""),
                        limit: int = 50):
    """R47: turns + tool calls for the web viewer. Full transcripts only
    with limit=0 (viewer fetches on demand)."""
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")
    if limit == 0:
        doc = await db.agent_sessions.find_one({"_id": agent_id})
        return {"messages": (doc or {}).get("messages", [])}
    turns = []
    async for t in db.runtime_turns.find({"agent_id": agent_id})             .sort("started", -1).limit(limit):
        turns.append({
            "id": t["_id"],
            "trigger": t.get("trigger"),
            "steps": t.get("steps", 0),
            "final": (t.get("final") or "")[:2000],
            "injections": [i[:500] for i in (t.get("injections") or [])],
            "started": t.get("started"),
            "ended": t.get("ended"),
        })
    return {"turns": turns}


# ---------------- agent hosts (R53) ----------------

from pydantic import BaseModel


class HostIn(BaseModel):
    name: str
    address: str
    port: int = 22
    ssh_user: str = "root"


@app.get("/internal/hosts/pubkey")
async def central_pubkey(x_service_token: str = Header(default="")):
    """The PUBLIC half of the central provisioning key. The UI shows it in
    the one-liner the operator runs on the agent box."""
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")
    from .hostprovision import _ensure_central_keypair
    _, pub = _ensure_central_keypair()
    return {"pubkey": pub}


@app.get("/internal/hosts")
async def list_hosts(x_service_token: str = Header(default="")):
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")
    out = []
    async for h in db.agent_hosts.find():
        out.append({"id": h["_id"], "name": h.get("name"),
                    "address": h.get("address"), "port": h.get("port", 22),
                    "ssh_user": h.get("ssh_user", "root"),
                    "status": h.get("status", "unknown"),
                    "last_log": (h.get("last_log") or "")[-800:],
                    "provisioned_at": h.get("provisioned_at")})
    return {"hosts": out}


@app.post("/internal/hosts", status_code=201)
async def add_host(body: HostIn, x_service_token: str = Header(default="")):
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")
    import secrets
    hid = f"host_{secrets.token_hex(6)}"
    await db.agent_hosts.insert_one(
        {"_id": hid, "name": body.name, "address": body.address,
         "port": body.port, "ssh_user": body.ssh_user,
         "status": "new", "created_at": _now_iso()})
    return {"id": hid}


@app.post("/internal/hosts/{host_id}/provision")
async def provision(host_id: str, x_service_token: str = Header(default="")):
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")
    h = await db.agent_hosts.find_one({"_id": host_id})
    if not h:
        raise HTTPException(404, "no such host")
    from .hostprovision import provision_host
    r = await provision_host(h)
    return r


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
