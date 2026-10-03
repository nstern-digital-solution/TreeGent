from fastapi import FastAPI

from . import s3
from .config import ensure_indexes
from .main import router

app = FastAPI(title="TreeGent files", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await ensure_indexes()
    try:
        await s3.ensure_bucket()
    except Exception as e:  # noqa: BLE001
        print(f"S3 unavailable (files will fail until configured): {e}")


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "files"}


app.include_router(router)
