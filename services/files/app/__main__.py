from fastapi import FastAPI

from . import storage
from .config import db, ensure_indexes
from .main import router
from .migrate import migrate_paths

app = FastAPI(title="TreeGent files", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    await ensure_indexes()
    m = await migrate_paths(db)     # idempotent, every boot (issue #51 B)
    if m.get("migrated"):
        print(f"[files] path migration: {m['migrated']} legacy file(s)")
    if m.get("deduped"):
        print(f"[files] path migration: dropped {m['deduped']} duplicate legacy file(s)")
    try:
        await storage.ensure_bucket()
    except Exception as e:  # noqa: BLE001
        print(f"byte storage unavailable (files will fail until configured): {e}")


@app.get("/health")
async def health():
    from .config import client
    await client.admin.command("ping")
    return {"ok": True, "service": "files"}

app.include_router(router)
