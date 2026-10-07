from fastapi import Header, HTTPException

from treegent_common.auth import actor_from_agent_key, authenticate
from treegent_common.perms import is_root_actor

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


def require_org_admin(*, allow_bootstrap: bool = False):
    """R62 sec-01: authorization for ORG-STRUCTURE mutations (actor create,
    reparent, delete). Resolving who you are is NOT authorization to edit
    the organization — a low-privilege credential must never restructure
    the hierarchy that superior secret/file access and mail approval
    routing depend on.

    Model enforced here (mirrors the treegent_common.perms root predicate):
    - only the ROOT HUMAN (kind=human, org.depth=0), on the CENTRAL tier
      (trusted same-host caller acting for that human), may mutate the org;
    - the AGENT tier (X-Agent-Key) can never mutate the org at all — agent
      keys are agent credentials, org management is a central-only
      capability (agent-host boundary: HTTPS-only, no Mongo credentials,
      no org writes);
    - narrow first-actor bootstrap exception: while the actors collection
      is EMPTY the central `boot` pseudo-actor may CREATE the first actor
      (create only — never reparent/delete).
    Deny by default."""

    async def dep(
        x_agent_key: str = Header(default=""),
        x_service_token: str = Header(default=""),
        x_actor_id: str = Header(default=""),
    ) -> dict:
        if await actor_from_agent_key(db.db, x_agent_key):
            raise HTTPException(403, "agent keys cannot manage the org")
        actor = await authenticate(db.db, settings.service_token,
                                   x_agent_key, x_service_token, x_actor_id)
        if actor.get("_id") == "boot":
            # authenticate() only mints this pseudo-actor while the actors
            # collection is empty; re-check so the exception stays narrow.
            if allow_bootstrap and await db.actors.count_documents({}) == 0:
                return actor
            raise HTTPException(403, "bootstrap closed")
        if is_root_actor(actor):
            return actor
        raise HTTPException(
            403, "org management requires the root human "
                 "(kind=human, org.depth=0)")
    return dep
