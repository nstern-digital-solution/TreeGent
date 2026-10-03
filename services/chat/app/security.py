from fastapi import Header, HTTPException

from treegent_common.auth import authenticate

from . import db
from .config import settings


async def require_service(
    x_agent_key: str = Header(default=""),
    x_service_token: str = Header(default=""),
    x_actor_id: str = Header(default=""),
) -> dict:
    """R45 dual auth: agent key derives identity server-side (claims
    ignored); shared token + actor id remains ONLY for central-tier
    callers (Meteor web server, same host)."""
    return await authenticate(db.db, settings.service_token,
                              x_agent_key, x_service_token, x_actor_id)


async def require_service_only(x_service_token: str = Header(default="")) -> None:
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")