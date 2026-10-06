from fastapi import FastAPI

from . import db
from .routers import actors, conversations, hosttier, inbox, internal, messages

app = FastAPI(title="TreeGent chat", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await db.ensure_indexes()


@app.get("/health")
async def health():
    await db.client.admin.command("ping")
    return {"ok": True, "service": "chat"}


app.include_router(actors.router)
app.include_router(conversations.router)
app.include_router(messages.router)
app.include_router(inbox.router)
app.include_router(internal.router)
app.include_router(hosttier.router)
