from fastapi import APIRouter, Depends

from .. import db
from ..security import require_service_only

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/org-weights")
async def org_weights(_: None = Depends(require_service_only)):
    """Actor id → org depth (0 = top of tree). Used by services/proxy to
    compute R32 priority (hierarchy weight). Service-token only."""
    out = {}
    async for a in db.actors.find({}, {"org.depth": 1}):
        out[a["_id"]] = (a.get("org") or {}).get("depth", 0)
    return out
