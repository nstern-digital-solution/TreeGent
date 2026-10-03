"""Files API. R40 permissions, same pattern as secrets: own-scope listing
(own + member), named reads allow superior reach-down, writes own/member.
Bytes live on the user-provided S3 host; Mongo holds metadata only."""
import secrets as pysecrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from pydantic import BaseModel
from treegent_common.auth import authenticate
from treegent_common.perms import can, principal_for, resource_for

from . import s3
from .config import db, files_col, settings

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


# ---------------- listing (own-scope) ----------------

@router.get("/files")
async def list_files(CLAIM_CALLER: str = "", q: str = "", _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    query: dict = {"$or": [{"owner": caller_id}, {"shared_with": caller_id}]}
    if q:
        query = {"$and": [query, {"name": {"$regex": q, "$options": "i"}}]}
    return [fpub(f) async for f in
            files_col.find(query).sort("updated_at", -1).limit(500)]


# ---------------- upload / download ----------------

@router.post("/files", status_code=201,
             )
async def upload(file: UploadFile, CLAIM_CALLER: str = "",
                 shared_with: str = "", _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "file too large (100 MB cap in v1)")
    fid = f"fil_{pysecrets.token_hex(8)}"
    s3_key = f"{caller_id}/{fid}/{file.filename}"
    content_type = file.content_type or "application/octet-stream"
    await s3.put(s3_key, data, content_type)
    doc = {"_id": fid, "name": file.filename, "size": len(data),
           "content_type": content_type, "owner": caller_id,
           "shared_with": [s for s in shared_with.split(",") if s],
           "s3_key": s3_key, "created_at": now(), "updated_at": now()}
    await files_col.insert_one(doc)
    return fpub(doc)


@router.get("/files/{file_id}/download")
async def download(file_id: str, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
    """Streams the bytes. files.read allows own/member/superior."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    await check(db, caller_id, "files.read", f)
    data = await s3.get(f["s3_key"])
    from fastapi import Response
    return Response(content=data, media_type=f.get("content_type"),
                    headers={"Content-Disposition":
                             f'attachment; filename="{f["name"]}"'})


@router.get("/files/{file_id}/url")
async def presigned_url(file_id: str, CLAIM_CALLER: str = "", expires_s: int = 900, _c: dict = Depends(caller_actor)):
    """Presigned URL straight from the user's S3 host (for big files /
    browser downloads) — permission-checked, then time-limited."""
    caller_id = _c["_id"]  # R45: derived, never claimed
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    await check(db, caller_id, "files.read", f)
    return {"url": await s3.presign_get(f["s3_key"], expires_s),
            "expires_s": expires_s}


@router.put("/files/{file_id}/share")
async def share(file_id: str, body: dict, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
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
async def delete_file(file_id: str, CLAIM_CALLER: str = "", _c: dict = Depends(caller_actor)):
    caller_id = _c["_id"]  # R45: derived, never claimed
    f = await files_col.find_one({"_id": file_id})
    if not f:
        raise HTTPException(404, "no such file")
    if caller_id != f["owner"]:
        raise HTTPException(403, "only the owner may delete")
    await s3.delete(f["s3_key"])
    await files_col.delete_one({"_id": file_id})
    return {"deleted": file_id}
