"""Org tree: parents, ancestor paths, cycle checks.

Stored shape per actor: org: {parent_id, ancestors: [root..parent], depth}.
`ancestors` is the stored-path optimization (R21): "is X a superior of Y"
is one array lookup, no recursion at read time.
"""
from fastapi import HTTPException

from . import db, idgen


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
    # issue #20: a pending approval SNAPSHOTs its approver (the requester's
    # then-superior) at send time — after a reparent the stale ex-approver
    # kept the queue entry and deciding rights while the new superior never
    # saw the send. Reassign the moved actor's PENDING approvals to its new
    # superior (decided rows are history and stay put) and move the wake
    # with them. Recompute-on-move was chosen over resolving the approver
    # from the parent at decide time: the inbox query and the decide gate
    # both key off approver_id, so a decide-time resolve would leave the
    # queue pointing at the ex-approver and hide it from the rightful one.
    reassigned = 0
    if node["parent_id"]:
        parent = await db.actors.find_one({"_id": node["parent_id"]})
        async for ap in db.approvals.find(
                {"requester_id": actor_id, "status": "pending",
                 "approver_id": {"$ne": node["parent_id"]}},
                {"_id": 1, "approver_id": 1}):
            await db.approvals.update_one(
                {"_id": ap["_id"]},
                {"$set": {"approver_id": node["parent_id"]}})
            # the stale wake points at the ex-approver's queue
            await db.wake_events.delete_many(
                {"reason": "approval", "approval_id": ap["_id"],
                 "agent_id": ap["approver_id"], "consumed": False})
            if parent and parent.get("kind") == "agent":
                # deterministic per-recipient wake id — idempotent across
                # repeated moves, never collides with the send-time wake
                await db.wake_events.update_one(
                    {"_id": f"wke_appr_{ap['_id']}_{node['parent_id']}"},
                    {"$setOnInsert": {
                        "agent_id": node["parent_id"], "reason": "approval",
                        "approval_id": ap["_id"], "created_at": idgen.now(),
                        "consumed": False}},
                    upsert=True)
            reassigned += 1
    return {"moved": actor_id, "subtree_updated": changed,
            "approvals_reassigned": reassigned}
