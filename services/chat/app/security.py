from fastapi import Header, HTTPException

from . import db
from .config import settings


async def require_service(
    x_service_token: str = Header(default=""),
    x_actor_id: str = Header(default=""),
) -> dict:
    """Trusted-caller auth for M1: the Meteor server presents the shared
    service token and identifies the acting actor. Replaced by per-actor
    tokens when services/control lands (R22)."""
    if x_service_token != settings.service_token:
        raise HTTPException(status_code=401, detail="bad service token")
    if not x_actor_id:
        raise HTTPException(status_code=400, detail="X-Actor-Id required")
    if x_actor_id == "boot":
        # bootstrap pseudo-actor: valid only while no real actors exist
        if await db.actors.count_documents({}) == 0:
            return {"_id": "boot", "username": "boot", "kind": "system",
                    "display_name": "bootstrap"}
        raise HTTPException(status_code=403, detail="bootstrap closed")
    actor = await db.actors.find_one({"_id": x_actor_id})
    if not actor:
        raise HTTPException(status_code=403, detail="unknown actor")
    return actor


async def require_service_only(x_service_token: str = Header(default="")) -> None:
    if x_service_token != settings.service_token:
        raise HTTPException(status_code=401, detail="bad service token")
