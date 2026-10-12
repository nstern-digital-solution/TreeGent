"""Regression tests for rosa-mar's two review findings on PR #52:
(1) "." path segments are canonicalized away, not distinct paths;
(2) one file per path — duplicate upload 409s, and legacy same-name
    siblings are deduped by the boot migration (oldest wins).
"""
from datetime import datetime, timezone

import httpx
import pytest

from conftest import MONGO_URL, db, hdr, require_mongo, running_server, seed_org, wipe

pytestmark = pytest.mark.skipif(not MONGO_URL,
                                reason="TG_TEST_MONGO_URL not set")
PORT = 18213


def _up(base, actor, name, content, path=None):
    url = f"{base}/files" + (f"?path={path}" if path else "")
    return httpx.post(url, headers=hdr(actor),
                      files={"file": (name, content, "text/plain")},
                      timeout=10)


def test_dot_segments_canonicalized():
    from app import paths
    assert paths.normalize_path("/home/a/./x") == "/home/a/x"
    assert paths.normalize_path("/home/./a") == "/home/a"
    assert paths.normalize_path("/home/a/b/././c") == "/home/a/b/c"
    assert paths.normalize_path("/.") == "/"
    assert paths.normalize_path("/home/a/../x") is None   # .. still rejected


def test_upload_duplicate_name_409_and_dot_paths():
    require_mongo()
    wipe()
    seed_org()
    with running_server(PORT) as base:
        r1 = _up(base, "agt_alice", "report.txt", b"v1", path="/home/alice")
        assert r1.status_code == 201, r1.text
        # same name in the same folder -> 409 (review finding 2)
        r2 = _up(base, "agt_alice", "report.txt", b"v2", path="/home/alice")
        assert r2.status_code == 409, r2.text
        # "." form of the same folder is the SAME path -> also 409 (finding 1)
        r3 = _up(base, "agt_alice", "report.txt", b"v3",
                 path="/home/alice/./")
        assert r3.status_code == 409, r3.text
        # a different folder accepts it
        r4 = _up(base, "agt_alice", "report.txt", b"v2",
                 path="/home/alice/other")
        assert r4.status_code == 201, r4.text
        # the "."-addressed upload lands in the canonical folder
        r5 = _up(base, "agt_alice", "note.txt", b"x", path="/home/alice/./sub")
        assert r5.status_code == 201, r5.text
        lst = httpx.get("http://127.0.0.1:%d/files" % PORT,
                        params={"path": "/home/alice/sub"},
                        headers=hdr("agt_alice"), timeout=10).json()
        assert [f["name"] for f in lst["files"]] == ["note.txt"]


def test_migration_dedupes_same_name_legacy_rows():
    require_mongo()
    wipe()
    seed_org()
    now = datetime.now(timezone.utc)
    db().files.insert_many([
        {"_id": "fil_old", "name": "report.txt", "size": 2,
         "content_type": "text/plain", "owner": "agt_alice", "shared_with": [],
         "s3_key": "agt_alice/fil_old/report.txt",
         "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "updated_at": now},
        {"_id": "fil_newer", "name": "report.txt", "size": 2,
         "content_type": "text/plain", "owner": "agt_alice", "shared_with": [],
         "s3_key": "agt_alice/fil_newer/report.txt",
         "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
         "updated_at": now},
    ])
    with running_server(PORT):
        d = db()
        assert d.files.count_documents({"owner": "agt_alice"}) == 1
        left = d.files.find_one({"owner": "agt_alice"})
        assert left["_id"] == "fil_old"            # oldest survived
        assert left["path"] == "/home/alice/report.txt"
