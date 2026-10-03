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
