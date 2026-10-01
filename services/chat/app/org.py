"""Org tree: parents, ancestor paths, cycle checks.

Stored shape per actor: org: {parent_id, ancestors: [root..parent], depth}.
`ancestors` is the stored-path optimization (R21): "is X a superior of Y"
is one array lookup, no recursion at read time.
"""
from fastapi import HTTPException

from . import db


def _node(parent_id: str | None, ancestors: list[str], depth: int) -> dict:
    return {"parent_id": parent_id, "ancestors": ancestors, "depth": depth}


async def compute_node(actor_id: str, new_parent_id: str | None) -> dict:
    if new_parent_id is None:
        return _node(None, [], 0)
    if new_parent_id == actor_id:
        raise HTTPException(400, "actor cannot be its own parent")
    parent = await db.actors.find_one({"_id": new_parent_id})
    if not parent:
        raise HTTPException(400, f"parent {new_parent_id} not found")
    # cycle check: actor must not appear in the parent's ancestor chain
    chain = [new_parent_id] + list(parent.get("org", {}).get("ancestors", []))
    if actor_id in chain:
        raise HTTPException(400, "would create a cycle in the org tree")
    porg = parent.get("org", {})
    return _node(new_parent_id, list(porg.get("ancestors", [])) + [new_parent_id],
                 int(porg.get("depth", 0)) + 1)


async def subtree_ids(actor_id: str) -> list[str]:
    """actor + all descendants (uses the ancestors path)."""
    ids = [actor_id]
    async for d in db.actors.find({"org.ancestors": actor_id}, {"_id": 1}):
        ids.append(d["_id"])
    return ids


async def move(actor_id: str, new_parent_id: str | None) -> dict:
    node = await compute_node(actor_id, new_parent_id)
    await db.actors.update_one({"_id": actor_id}, {"$set": {"org": node}})
    # recompute paths of the whole subtree (parents only grow by prefix)
    changed = 1
    frontier = [actor_id]
    while frontier:
        nxt = []
        async for child in db.actors.find({"org.parent_id": {"$in": frontier}},
                                          {"_id": 1, "org": 1}):
            # recompute from the (already updated) parent chain:
            child_node = await compute_node(child["_id"], child["org"]["parent_id"])
            await db.actors.update_one({"_id": child["_id"]},
                                       {"$set": {"org": child_node}})
            nxt.append(child["_id"])
            changed += 1
        frontier = nxt
    return {"moved": actor_id, "subtree_updated": changed}
