"""R45 dual auth, one implementation for every service.

Two tiers, deliberately:
- AGENT tier: X-Agent-Key. Identity = the key's owner, looked up
  server-side. Any claimed id/header/param is IGNORED. This is the only
  credential an agent host ever holds.
- CENTRAL tier: X-Service-Token + declared actor id. For trusted
  same-host callers (the Meteor web server acting for a logged-in
  human). The token must never leave the central services host.

An agent key outranks the central tier when both are presented.
"""
from fastapi import Header, HTTPException


async def actor_from_agent_key(db, x_agent_key: str) -> dict | None:
    if not x_agent_key:
        return None
    k = await db.agent_keys.find_one({"_id": x_agent_key})
    if not k or k.get("revoked"):
        return None
    a = await db.actors.find_one({"_id": k["agent_id"]})
    return a


async def authenticate(
    db,
    service_token: str,
    x_agent_key: str = Header(default=""),
    x_service_token: str = Header(default=""),
    x_actor_id: str = Header(default=""),
) -> dict:
    """Resolve the calling actor. Raises 401/403 when no tier applies."""
    actor = await actor_from_agent_key(db, x_agent_key)
    if actor:
        return actor
    if x_service_token and x_service_token == service_token:
        if not x_actor_id:
            raise HTTPException(400, "X-Actor-Id required for central tier")
        if x_actor_id == "boot":
            if await db.actors.count_documents({}) == 0:
                return {"_id": "boot", "username": "boot", "kind": "system",
                        "display_name": "bootstrap"}
            raise HTTPException(403, "bootstrap closed")
        actor = await db.actors.find_one({"_id": x_actor_id})
        if not actor:
            raise HTTPException(403, "unknown actor")
        return actor
    raise HTTPException(401, "no valid credential (agent key or service token)")
