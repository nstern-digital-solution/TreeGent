"""Files API. R40 permissions, same pattern as secrets: own-scope listing
(own + member), named reads allow superior reach-down, writes own/subtree.
Bytes live on the configured storage backend (S3 in prod, in-memory under
TG_DEV=1); Mongo holds metadata only.

Issue #51: the real hierarchical file system — every doc carries an
absolute `path` (see paths.normalize_path), folders are first-class docs,
listings return direct children of a path, and writes are scoped to the
caller's own subtree (/home/<username> or a top folder they created).
"""
import re
import secrets as pysecrets
from datetime import datetime, timezone

from fastapi import (APIRouter, Depends, Header, HTTPException, Response,
                     UploadFile)
from treegent_common.auth import authenticate
from treegent_common.perms import (can, is_root_actor, principal_for,
                                   resource_for)

from . import paths, storage
from .config import db, files_col, folders_col, settings
from .migrate import ensure_folder

router = APIRouter(tags=["files"])


async def caller_actor(
    x_agent_key: str = Header(default=""),
    x_service_token: str = Header(default=""),
    x_actor_id: str = Header(default=""),
) -> dict:
    return await authenticate(db, settings.service_token,
                              x_agent_key, x_service_token, x_actor_id)


async def require_service(x_service_token: str = Header(default="")) -> None:
    if x_service_token != settings.service_token:
        raise HTTPException(401, "bad service token")


def now():
    return datetime.now(timezone.utc)


def fpub(f: dict) -> dict:
    return {"id": f["_id"], "name": f["name"], "size": f.get("size"),
            "content_type": f.get("content_type"), "owner": f["owner"],
            "shared_with": f.get("shared_with", []),
            "created_at": f.get("created_at"),
            "updated_at": f.get("updated_at")}


async def check(db_, caller_id: str, perm: str, f: dict) -> None:
    p = await principal_for(db_, caller_id)
    r = await resource_for(db_, [f["owner"]], f.get("shared_with", []))
    if not await can(db_, p, perm, r):
        raise HTTPException(403, f"not allowed ({perm})")


# ---------------- subtree model (issue #51 B) ----------------

def home_of(username: str) -> str:
    return f"/home/{username}"


def _in_home(username: str, path: str) -> bool:
    """path is the caller's home or inside it (their own subtree)."""
    home = home_of(username)
    return path == home or path.startswith(home + "/")


def _top_folder(path: str) -> str:
    """The tree's top folder: /projects/x -> /projects."""
    segs = [s for s in path.split("/") if s]
    return f"/{segs[0]}" if segs else paths.ROOT


async def _can_create(caller_id: str, username: str, path: str) -> bool:
    """May the caller create (folders/files) at path? Own home subtree, or
    a non-personal tree whose top folder is missing (fresh claim) or is
    already theirs. /home/* is a reserved personal namespace: only the
    home owner (above) or root writes there."""
    if _in_home(username, path):
        return True
    if path.startswith("/home/"):
        return False
    top = _top_folder(path)
    doc = await folders_col.find_one({"path": top})
    return doc is None or doc.get("owner") == caller_id


async def _owns_subtree(caller_id: str, username: str, path: str) -> bool:
    """Owns the prefix subtree (share/file write rights): the home subtree,
    or a non-personal tree whose top folder exists and is theirs ('they
    created the top folder')."""
    if _in_home(username, path):
        return True
    if path.startswith("/home/"):
        return False
    doc = await folders_col.find_one({"path": _top_folder(path)})
    return bool(doc and doc.get("owner") == caller_id)


def _visible_or(username: str, caller_id: str) -> list[dict]:
    """Listing visibility (R40 own-scope — superiors are NOT included):
    own | member | inside my home | the navigation chain leading to my home
    (so every user can browse down into their own subtree)."""
    home = home_of(username)
    chain = [d for d in paths.ancestors(home) if d != paths.ROOT]
    return [{"owner": caller_id}, {"shared_with": caller_id},
            {"path": home},
            {"path": {"$regex": "^" + re.escape(home) + "/"}},
            {"path": {"$in": chain + [home]}}]


def _child_pattern(folder: str) -> str:
    base = folder.rstrip("/") if folder != paths.ROOT else ""
    return "^" + re.escape(base) + "/[^/]+$"


def _under_pattern(path: str) -> str:
    return "^" + re.escape(path.rstrip("/")) + "/.+"


# ---------------- listing (own-scope) ----------------

@router.get("/files")
async def list_files(CLAIM_CALLER: str = "", q: str = "", path: str = "",
                     _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    username = _c.get("username") or caller_id
    if path:
        folder = paths.normalize_path(path)
        if folder is None:
            raise HTTPException(400, "bad path")
        sel: dict = {"path": {"$regex": _child_pattern(folder)},
                     "$or": _visible_or(username, caller_id)}
        if q:
            sel = {"$and": [sel, {"name": {"$regex": q, "$options": "i"}}]}
        folders = [{"name": paths.basename(d["path"]), "path": d["path"],
                    "owner": d.get("owner")}
                   async for d in folders_col.find(sel).sort("path", 1)
                   .limit(500)]
        files = [fpub(f) async for f in
                 files_col.find(sel).sort("name", 1).limit(500)]
        return {"folders": folders, "files": files}
    # DEPRECATED (issue #51): legacy flat own+shared listing, kept for old
    # web clients until they browse by path — use ?path= instead.
    query: dict = {"$or": _visible_or(username, caller_id)}
    if q:
        query = {"$and": [query, {"name": {"$regex": q, "$options": "i"}}]}
    return [fpub(f) async for f in
            files_col.find(query).sort("updated_at", -1).limit(500)]


# ---------------- upload / download ----------------

@router.post("/files", status_code=201)
async def upload(file: UploadFile, CLAIM_CALLER: str = "",
                 shared_with: str = "", path: str = "",
                 _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    username = _c.get("username") or caller_id
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "file too large (100 MB cap in v1)")
    name = file.filename or ""
    if not name or not paths.SEGMENT_RE.fullmatch(name):
        raise HTTPException(400, "file name not allowed in paths")
    if path:
        folder = paths.normalize_path(path)
        if folder is None:
            raise HTTPException(400, "bad path")
    else:
        folder = home_of(username)   # default: the owner's home
    if not await _can_create(caller_id, username, folder):
        raise HTTPException(403, "not your subtree")
    full = paths.normalize_path(
        (folder.rstrip("/") if folder != paths.ROOT else "") + "/" + name)
    if full is None:
        raise HTTPException(400, "bad path")
    members = [s for s in shared_with.split(",") if s]
    if members:
        await _validate_actors(members)
    fid = f"fil_{pysecrets.token_hex(8)}"
    s3_key = f"{caller_id}/{fid}/{name}"
    content_type = file.content_type or "application/octet-stream"
    await storage.put(s3_key, data, content_type)
    # the folder chain exists so listings/deletes see a coherent tree
    for d in [d for d in paths.ancestors(full) if d != paths.ROOT]:
        await ensure_folder(folders_col, d, caller_id)
    await ensure_folder(folders_col, paths.dirname(full), caller_id)
    doc = {"_id": fid, "name": name, "size": len(data),
           "content_type": content_type, "owner": caller_id,
           "shared_with": members,
           "s3_key": s3_key, "path": full, "kind": "file",
           "created_at": now(), "updated_at": now()}
    await files_col.insert_one(doc)
    return fpub(doc)


async def _validate_actors(actor_ids: list[str]) -> None:
    """Every named actor must exist (issue #51 C) — 400 listing them."""
    if not actor_ids:
        return
    found = {a["_id"] async for a in
             db.actors.find({"_id": {"$in": actor_ids}}, {"_id": 1})}
    missing = sorted(set(actor_ids) - found)
    if missing:
        raise HTTPException(400, f"unknown actors: {', '.join(missing)}")


async def _can_read(caller_id: str, username: str, f: dict) -> bool:
    """Read access: own | member | superior named-read (R40 rule engine) |
    inside the caller's own subtree (files others drop into your home are
    yours to read)."""
    try:
        await check(db, caller_id, "files.read", f)
        return True
    except HTTPException:
        pass
    return _in_home(username, f.get("path") or "")


@router.get("/files/{file_id}/download")
async def download(file_id: str, CLAIM_CALLER: str = "",
                   _c: dict = Depends(caller_actor)):
    """Streams the bytes. files.read allows own/member/superior."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    username = _c.get("username") or caller_id
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    if not await _can_read(caller_id, username, f):
        raise HTTPException(403, "not allowed (files.read)")
    data = await storage.get(f["s3_key"])
    return Response(content=data, media_type=f.get("content_type"),
                    headers={"Content-Disposition":
                             f'attachment; filename="{f["name"]}"'})


@router.get("/files/{file_id}/url")
async def presigned_url(file_id: str, CLAIM_CALLER: str = "",
                        expires_s: int = 900, _c: dict = Depends(caller_actor)):
    """Presigned URL straight from the byte store (for big files / browser
    downloads) — permission-checked, then time-limited."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    username = _c.get("username") or caller_id
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    if not await _can_read(caller_id, username, f):
        raise HTTPException(403, "not allowed (files.read)")
    return {"url": await storage.presign_get(f["s3_key"], expires_s),
            "expires_s": expires_s}


@router.put("/files/{file_id}/share")
async def share(file_id: str, body: dict, CLAIM_CALLER: str = "",
                _c: dict = Depends(caller_actor)):
    """Owner (or member) shares with more actors."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    await check(db, caller_id, "files.write", f)
    new = sorted(set(f.get("shared_with", [])) | set(body.get("with", [])))
    await files_col.update_one({"_id": file_id},
                               {"$set": {"shared_with": new,
                                         "updated_at": now()}})
    return {"id": file_id, "shared_with": new}


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, CLAIM_CALLER: str = "",
                      _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    username = _c.get("username") or caller_id
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    # write-side strictest op (issue #51 C): subtree owner or root
    if (caller_id != f["owner"] and not is_root_actor(_c)
            and not await _owns_subtree(caller_id, username,
                                        f.get("path") or "")):
        raise HTTPException(403, "only the owner may delete")
    await storage.delete(f["s3_key"])
    await files_col.delete_one({"_id": file_id})
    return {"deleted": file_id}


# ---------------- folders (issue #51 B) ----------------

@router.post("/folders")
async def make_folder(body: dict, response: Response, CLAIM_CALLER: str = "",
                      _c: dict = Depends(caller_actor)):
    """mkdir -p: create every missing folder on the path (idempotent).
    201 with the created list; 200 when everything already existed."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    username = _c.get("username") or caller_id
    p = paths.normalize_path((body or {}).get("path") or "")
    if p is None or p == paths.ROOT:
        raise HTTPException(400, "bad path")
    if not await _can_create(caller_id, username, p):
        raise HTTPException(403, "not your subtree")
    created = []
    for d in [d for d in paths.ancestors(p) if d != paths.ROOT] + [p]:
        if await ensure_folder(folders_col, d, caller_id):
            created.append(d)
    response.status_code = 201 if created else 200
    return {"path": p, "created": created}


@router.delete("/folders/{folder_path:path}")
async def delete_folder(folder_path: str, CLAIM_CALLER: str = "",
                        _c: dict = Depends(caller_actor)):
    """Only empty folders go away (409 otherwise); owner or root."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    # the :path converter strips the leading "/" — restore it
    p = paths.normalize_path("/" + folder_path.lstrip("/"))
    if p is None or p == paths.ROOT:
        raise HTTPException(400, "bad path")
    doc = await folders_col.find_one({"path": p})
    if not doc:
        raise HTTPException(404, "no such folder")
    if caller_id != doc.get("owner") and not is_root_actor(_c):
        raise HTTPException(403, "only the owner may delete")
    busy = _under_pattern(p)
    if (await folders_col.count_documents({"path": {"$regex": busy}})
            or await files_col.count_documents({"path": {"$regex": busy}})):
        raise HTTPException(409, "folder not empty")
    await folders_col.delete_one({"_id": doc["_id"]})
    return {"deleted": p}
