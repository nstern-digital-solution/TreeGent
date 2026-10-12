"""Share semantics over the real service (issue #51 C):

- FOLDER SHARE INHERITANCE: alice owns /t1/x and shares it with bob; files
  alice adds afterwards are visible to bob IMMEDIATELY (no re-share)
  because access is resolved per request over the covered prefix.
- member re-share is rejected (the audit hole): bob, a share member, may
  neither create shares nor change per-file member lists.
- per-file share is SET-REPLACEMENT: add -> replace -> empty, and access
  follows the list exactly.
- unknown actors in `with` are a 400 that names them.
- R40: superior named reads pass, superior LISTINGS stay own-scope.
- revocation via DELETE /shares/{id} is immediate.

Every test builds its own fresh tree (/<tag>/x) so share state never
crosses between tests.
"""
import pytest
import httpx

from conftest import hdr, MONGO_URL, require_mongo, running_server, seed_org, wipe

pytestmark = pytest.mark.skipif(not MONGO_URL,
                                reason="TG_TEST_MONGO_URL not set")
PORT = 18211


@pytest.fixture(scope="module")
def server():
    require_mongo()
    wipe()
    seed_org()
    with running_server(PORT) as base:
        yield base


def _tree(server, tag):
    """alice's own tree /<tag>/x holding one file."""
    r = httpx.post(f"{server}/folders", json={"path": f"/{tag}/x"},
                   headers=hdr("agt_alice"))
    assert r.status_code == 201, r.text
    r = httpx.post(f"{server}/files?path=/{tag}/x", headers=hdr("agt_alice"),
                   files={"file": ("early.txt", b"e", "text/plain")})
    assert r.status_code == 201, r.text
    return f"/{tag}/x", r.json()["id"]


def test_folder_share_inheritance(server):
    path, fid_early = _tree(server, "t1")
    r = httpx.post(f"{server}/shares", json={"path": path, "with": ["agt_bob"]},
                   headers=hdr("agt_alice"))
    assert r.status_code == 201, r.text
    assert r.json()["path_prefix"] == path
    # bob sees the pre-existing file AND the navigation chain to it
    r = httpx.get(f"{server}/files", params={"path": path},
                  headers=hdr("agt_bob"))
    assert fid_early in [f["id"] for f in r.json()["files"]]
    r = httpx.get(f"{server}/files", params={"path": "/t1"},
                  headers=hdr("agt_bob"))
    assert path in [d["path"] for d in r.json()["folders"]]
    # THE inheritance case: alice adds a file AFTER the share was created —
    # bob must see it without any re-share
    r = httpx.post(f"{server}/files", params={"path": path},
                   headers=hdr("agt_alice"),
                   files={"file": ("late.txt", b"l", "text/plain")})
    fid_late = r.json()["id"]
    r = httpx.get(f"{server}/files", params={"path": path},
                  headers=hdr("agt_bob"))
    assert fid_late in [f["id"] for f in r.json()["files"]]
    r = httpx.get(f"{server}/files/{fid_late}/download", headers=hdr("agt_bob"))
    assert r.status_code == 200 and r.content == b"l"
    # but a share member still can NOT write into the shared tree
    r = httpx.post(f"{server}/files", params={"path": path},
                   headers=hdr("agt_bob"),
                   files={"file": ("intruder.txt", b"x", "text/plain")})
    assert r.status_code == 403


def test_member_reshare_rejected(server):
    path, fid = _tree(server, "t2")
    r = httpx.post(f"{server}/shares", json={"path": path, "with": ["agt_bob"]},
                   headers=hdr("agt_alice"))
    assert r.status_code == 201
    # bob is now a MEMBER of the share — mutating shares is owner-only
    # (subtree owner + root), never a member (the audit hole)
    r = httpx.post(f"{server}/shares", json={"path": path, "with": ["hum_root"]},
                   headers=hdr("agt_bob"))
    assert r.status_code == 403
    r = httpx.put(f"{server}/files/{fid}/share", json={"with": ["hum_root"]},
                  headers=hdr("agt_bob"))
    assert r.status_code == 403
    # ... and neither can a peer who owns nothing here
    r = httpx.post(f"{server}/shares", json={"path": path, "with": ["agt_bob"]},
                   headers=hdr("agt_boss"))
    assert r.status_code == 403


def test_unknown_actor_in_with_is_400(server):
    path, fid = _tree(server, "t3")
    r = httpx.post(f"{server}/shares",
                   json={"path": path, "with": ["agt_bob", "ghost"]},
                   headers=hdr("agt_alice"))
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"] and "agt_bob" not in r.json()["detail"]
    r = httpx.put(f"{server}/files/{fid}/share", json={"with": ["ghost"]},
                  headers=hdr("agt_alice"))
    assert r.status_code == 400 and "ghost" in r.json()["detail"]


def test_per_file_share_set_replacement(server):
    path, fid = _tree(server, "t4")     # NO folder share here
    # add
    r = httpx.put(f"{server}/files/{fid}/share", json={"with": ["agt_carol"]},
                  headers=hdr("agt_alice"))
    assert r.status_code == 200 and r.json()["shared_with"] == ["agt_carol"]
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_carol")).status_code == 200
    # replace: carol OUT, bob IN — the old member loses access outright
    r = httpx.put(f"{server}/files/{fid}/share", json={"with": ["agt_bob"]},
                  headers=hdr("agt_alice"))
    assert r.json()["shared_with"] == ["agt_bob"]
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_carol")).status_code == 403
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_bob")).status_code == 200
    # empty list = full revoke
    r = httpx.put(f"{server}/files/{fid}/share", json={"with": []},
                  headers=hdr("agt_alice"))
    assert r.json()["shared_with"] == []
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_bob")).status_code == 403
    # body without `with` is a 400 (a silent merge would re-open the hole)
    r = httpx.put(f"{server}/files/{fid}/share", json={},
                  headers=hdr("agt_alice"))
    assert r.status_code == 400


def test_revocation_is_immediate(server):
    path, fid = _tree(server, "t5")
    sid = httpx.post(f"{server}/shares",
                     json={"path": path, "with": ["agt_bob"]},
                     headers=hdr("agt_alice")).json()["id"]
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_bob")).status_code == 200
    # only the creator (or root) may revoke
    assert httpx.delete(f"{server}/shares/{sid}",
                        headers=hdr("agt_bob")).status_code == 403
    assert httpx.delete(f"{server}/shares/{sid}",
                        headers=hdr("agt_alice")).status_code == 200
    # per-request resolution: the very next call finds nothing
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_bob")).status_code == 403
    r = httpx.get(f"{server}/files", params={"path": path},
                  headers=hdr("agt_bob"))
    assert r.json() == {"folders": [], "files": []}


def test_superior_named_read_but_own_scope_listing(server):
    path, fid = _tree(server, "t6")
    # alice's boss: superior named read passes (R40 rule engine)
    assert httpx.get(f"{server}/files/{fid}/download",
                     headers=hdr("agt_boss")).status_code == 200
    # ... but the superior's LISTINGS never return subordinates' items
    r = httpx.get(f"{server}/files", params={"path": path},
                  headers=hdr("agt_boss"))
    assert r.json() == {"folders": [], "files": []}
    r = httpx.get(f"{server}/files", headers=hdr("agt_boss"))
    assert all(f["id"] != fid for f in r.json())
    r = httpx.get(f"{server}/files", headers=hdr("hum_root"))
    assert all(f["id"] != fid for f in r.json())


def test_presign_permission_still_gates(server):
    path, fid = _tree(server, "t7")
    r = httpx.get(f"{server}/files/{fid}/url", headers=hdr("agt_alice"))
    assert r.status_code == 200 and r.json()["url"]
    # a stranger gets nothing — the presigned URL is behind the same checks
    assert httpx.get(f"{server}/files/{fid}/url",
                     headers=hdr("agt_bob")).status_code == 403
    # superior named-read reaches down
    assert httpx.get(f"{server}/files/{fid}/url",
                     headers=hdr("hum_root")).status_code == 200


def test_shares_listing_scope(server):
    path, _ = _tree(server, "t8")
    sid = httpx.post(f"{server}/shares",
                     json={"path": path, "with": ["agt_bob"]},
                     headers=hdr("agt_alice")).json()["id"]
    # creator sees it (own subtree)
    assert sid in [s["id"] for s in
                   httpx.get(f"{server}/shares", headers=hdr("agt_alice")).json()]
    # member sees the share covering paths they can see
    assert sid in [s["id"] for s in
                   httpx.get(f"{server}/shares", headers=hdr("agt_bob")).json()]
    # an unrelated actor does not
    assert sid not in [s["id"] for s in
                       httpx.get(f"{server}/shares", headers=hdr("agt_boss")).json()]
    # root sees everything
    assert sid in [s["id"] for s in
                   httpx.get(f"{server}/shares", headers=hdr("hum_root")).json()]
