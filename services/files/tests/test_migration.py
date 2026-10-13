"""Boot migration (issue #51 B): every legacy file doc without `path` gets
`/home/<owner-username>/<name>`, folder docs are ensured for every
/home/<username>, /home itself and every migrated ancestor — and the whole
pass is IDEMPOTENT (a second boot changes nothing).

The legacy docs are seeded BEFORE the server boots so the real startup
migration path is exercised, exactly like a deployed box upgrading.
"""
from datetime import datetime, timezone

import httpx
import pytest

from conftest import MONGO_URL, db, hdr, require_mongo, running_server, seed_org, wipe

pytestmark = pytest.mark.skipif(not MONGO_URL,
                                reason="TG_TEST_MONGO_URL not set")
PORT = 18212


def _seed_legacy():
    wipe()
    seed_org()
    now = datetime.now(timezone.utc)
    db().files.insert_many([
        # legacy doc: no path, no kind
        {"_id": "fil_legacy1", "name": "legacy.txt", "size": 3,
         "content_type": "text/plain", "owner": "agt_alice", "shared_with": [],
         "s3_key": "agt_alice/fil_legacy1/legacy.txt",
         "created_at": now, "updated_at": now},
        # legacy doc with a name that is NOT a valid path segment — the
        # migration still assigns the documented verbatim path
        {"_id": "fil_legacy2", "name": "legacy (old).txt", "size": 1,
         "content_type": "text/plain", "owner": "agt_bob", "shared_with": [],
         "s3_key": "agt_bob/fil_legacy2/x", "created_at": now,
         "updated_at": now},
    ])


def test_migration_assigns_paths_and_folders():
    require_mongo()
    _seed_legacy()
    with running_server(PORT):
        d = db()
        f1 = d.files.find_one({"_id": "fil_legacy1"})
        assert f1["path"] == "/home/alice/legacy.txt"
        assert f1["kind"] == "file"
        f2 = d.files.find_one({"_id": "fil_legacy2"})
        assert f2["path"] == "/home/bob/legacy (old).txt"   # name verbatim
        folders = {x["path"]: x for x in d.folders.find({})}
        # /home is nobody's subtree; every actor's home is their own
        assert folders["/home"]["owner"] == "system"
        assert folders["/home/alice"]["owner"] == "agt_alice"
        assert folders["/home/bob"]["owner"] == "agt_bob"
        for username in ("root", "boss", "carol"):
            assert f"/home/{username}" in folders
        # migrated file lands in the owner's home listing
        r = httpx.get(f"http://127.0.0.1:{PORT}/files",
                      params={"path": "/home/alice"}, headers=hdr("agt_alice"))
        assert "legacy.txt" in [f["name"] for f in r.json()["files"]]
        # NOTE: the migration is metadata repair only — legacy s3_key bytes
        # are left in place and are not re-uploaded.


def test_migration_idempotent_on_reboot():
    require_mongo()
    before_folders = sorted(x["path"] for x in db().folders.find({}))
    before_files = {x["_id"]: x.get("path") for x in db().files.find({})}
    assert before_files["fil_legacy1"] == "/home/alice/legacy.txt"  # test 1 ran
    with running_server(PORT):
        d = db()
        after_folders = sorted(x["path"] for x in d.folders.find({}))
        after_files = {x["_id"]: x.get("path") for x in d.files.find({})}
        assert after_folders == before_folders   # no duplicate/extra folders
        assert after_files == before_files        # paths untouched
        # every actor still has exactly one home folder doc
        homes = [x for x in d.folders.find({"path": {"$regex": "^/home/[^/]+$"}})]
        assert len({x["path"] for x in homes}) == 5   # root, boss, alice, bob, carol
