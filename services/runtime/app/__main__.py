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
