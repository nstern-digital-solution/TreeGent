from fastapi import Header, HTTPException

from .config import settings


async def require_service(x_service_token: str = Header(default="")) -> None:
    """Trusted-caller auth for admin/tool endpoints (Meteor server, agent
    runtime). Same M1 model; replaced by per-actor tokens with control."""
    if x_service_token != settings.service_token:
        raise HTTPException(status_code=401, detail="bad service token")
