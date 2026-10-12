"""Boot migration for the hierarchical file system (issue #51 B).

Idempotent, runs at every service start:
- every file doc without `path` gets `/home/<owner-username>/<name>`
  (name verbatim — migration is data repair, not an upload path);
- a folder doc is ensured for every `/home/<username>` (owner = the actor),
  for `/home` itself (owner sentinel "system" — the personal namespace is
  nobody's subtree), and for every ancestor of every migrated path
  (owner = the migrated file's owner, "who got there first").
"""
from datetime import datetime, timezone
import secrets as pysecrets

from .paths import ROOT, ancestors


def _now():
    return datetime.now(timezone.utc)


async def ensure_folder(folders_col, path: str, owner: str) -> bool:
    """Ensure a folder doc exists at path. True when one was created."""
    if path == ROOT:
        return False
    if await folders_col.find_one({"path": path}):
        return False
    await folders_col.insert_one({"_id": f"fld_{pysecrets.token_hex(8)}",
                                  "kind": "folder", "path": path,
                                  "owner": owner, "created_at": _now()})
    return True


async def migrate_paths(db) -> dict:
    files_col, folders_col, actors = db.files, db.folders, db.actors
    # /home is nobody's subtree (owner sentinel) — seeded first so a
    # migrated file's ancestor pass can never claim it for a user
    await ensure_folder(folders_col, "/home", "system")
    # file docs always carry the tree fields, however old they are
    await files_col.update_many({"kind": {"$exists": False}},
                                {"$set": {"kind": "file"}})
    migrated = 0
    async for f in files_col.find({"path": {"$exists": False}}):
        a = await actors.find_one({"_id": f.get("owner")}, {"username": 1})
        username = (a or {}).get("username") or f.get("owner") or "unknown"
        name = f.get("name") or f["_id"]
        path = f"/home/{username}/{name}"
        await files_col.update_one({"_id": f["_id"]},
                                   {"$set": {"path": path, "kind": "file"}})
        for d in ancestors(path):
            await ensure_folder(folders_col, d, f.get("owner") or "system")
        migrated += 1
    # home folders for EVERY actor (the personal namespace is always browsable)
    async for a in actors.find({}, {"username": 1}):
        if a.get("username"):
            await ensure_folder(folders_col, f"/home/{a['username']}",
                                a["_id"])
    return {"migrated": migrated}
