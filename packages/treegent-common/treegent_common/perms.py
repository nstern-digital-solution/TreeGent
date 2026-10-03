"""TreeGent permission engine (R40) — rules are DATA rows, code enforces.

Rule row:   {_id: "secrets.read", allow: ["own", "member", "superior"]}
Principal:  {actor_id, is_root}           — same rules for humans and agents
Resource:   {owners: [...], members: [...], ancestors: [...]}
            ancestors = actor ids ABOVE the resource's owners/members

Predicates (the whole vocabulary):
    own      principal owns the resource
    member   resource is shared with the principal (e.g. sales@)
    superior principal is above an owner/member in the org (reach-down)
    root     principal is the org-root human (infrastructure only)

Deny by default: no rule row or no matching predicate -> deny.
"""


async def can(db, principal: dict, perm: str, resource: dict) -> bool:
    row = await db.permissions.find_one({"_id": perm})
    if not row:
        return False
    allow = row.get("allow") or []
    a = principal.get("actor_id")
    if "own" in allow and a in (resource.get("owners") or []):
        return True
    if "member" in allow and a in (resource.get("members") or []):
        return True
    if "superior" in allow and a in (resource.get("ancestors") or []):
        return True
    if "root" in allow and principal.get("is_root"):
        return True
    return False


async def principal_for(db, actor_id: str) -> dict:
    """Principal from an actor row. Root = the human at org depth 0."""
    a = await db.actors.find_one({"_id": actor_id}) or {}
    org = a.get("org") or {}
    return {"actor_id": actor_id,
            "is_root": a.get("kind") == "human" and (org.get("depth") or 0) == 0}


async def resource_for(db, owner_ids: list[str],
                       member_ids: list[str] | None = None) -> dict:
    """Build a resource doc: owners, members, and the union of their
    org ancestors (everyone above the resource)."""
    members = list(member_ids or [])
    anc: set[str] = set()
    for oid in list(owner_ids) + members:
        a = await db.actors.find_one({"_id": oid}, {"org": 1})
        anc.update(((a or {}).get("org") or {}).get("ancestors") or [])
    anc.difference_update(owner_ids)
    return {"owners": list(owner_ids), "members": members,
            "ancestors": sorted(anc)}


RULE_SEEDS = [
    # mail (M3) — list = own-scope ONLY; read = named open (superior may reach down)
    {"_id": "mailboxes.list", "allow": ["own", "member"]},
    {"_id": "mailboxes.read", "allow": ["own", "member", "superior"]},
    {"_id": "mail.send_as", "allow": ["own", "member"]},
    {"_id": "approvals.read", "allow": ["own"]},
    {"_id": "approvals.decide", "allow": ["own"]},
    # infrastructure (root human only)
    {"_id": "providers.read", "allow": ["root"]},
    {"_id": "providers.edit", "allow": ["root"]},
    {"_id": "catalog.read", "allow": ["root"]},
    {"_id": "classes.read", "allow": ["root"]},
    {"_id": "classes.edit", "allow": ["root"]},
    {"_id": "permissions.edit", "allow": ["root"]},
    # usage / history
    {"_id": "usage.read", "allow": ["own", "superior"]},
    {"_id": "history.read", "allow": ["own", "superior"]},
    # M4-ready (secrets, files) — same shape, adopted when built
    {"_id": "secrets.read", "allow": ["own", "member", "superior"]},
    {"_id": "secrets.write", "allow": ["own", "member"]},
    {"_id": "files.read", "allow": ["own", "member", "superior"]},
    {"_id": "files.write", "allow": ["own", "member"]},
]
