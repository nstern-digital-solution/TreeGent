from fastapi import FastAPI

from .config import ensure_indexes
from .main import router

app = FastAPI(title="TreeGent secrets", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await ensure_indexes()


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "secrets"}


app.include_router(router)
