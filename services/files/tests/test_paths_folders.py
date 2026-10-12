"""Paths + folders over the real service (issue #51 B): mkdir -p
idempotency, direct-children listings, uploads into folders, the
traversal/charset guards at the API boundary, empty-folder delete, and
the subtree write rule (a member/peer can NOT write into someone else's
tree — bob uploads into /projects/x fails even when he can see it)."""
import pytest
import httpx

from conftest import hdr, MONGO_URL, require_mongo, running_server, seed_org, wipe

pytestmark = pytest.mark.skipif(not MONGO_URL,
                                reason="TG_TEST_MONGO_URL not set")
PORT = 18210


@pytest.fixture(scope="module")
def server():
    require_mongo()
    wipe()
    seed_org()
    with running_server(PORT) as base:
        yield base


def test_mkdir_p_idempotent(server):
    r = httpx.post(f"{server}/folders", json={"path": "/projects/x/y"},
                   headers=hdr("agt_alice"))
    assert r.status_code == 201, r.text
    assert r.json() == {"path": "/projects/x/y",
                        "created": ["/projects", "/projects/x", "/projects/x/y"]}
    r = httpx.post(f"{server}/folders", json={"path": "/projects/x/y"},
                   headers=hdr("agt_alice"))
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "/projects/x/y", "created": []}


def test_listing_direct_children_only(server):
    r = httpx.post(f"{server}/files?path=/projects/x", headers=hdr("agt_alice"),
                   files={"file": ("a.txt", b"aaa", "text/plain")})
    assert r.status_code == 201, r.text
    r = httpx.post(f"{server}/files?path=/projects/x/y",
                   headers=hdr("agt_alice"),
                   files={"file": ("deep.txt", b"d", "text/plain")})
    assert r.status_code == 201, r.text
    r = httpx.get(f"{server}/files?path=/projects/x", headers=hdr("agt_alice"))
    got = r.json()
    assert [d["name"] for d in got["folders"]] == ["y"]
    assert [f["name"] for f in got["files"]] == ["a.txt"]     # NOT deep.txt
    r = httpx.get(f"{server}/files?path=/projects", headers=hdr("agt_alice"))
    assert [d["name"] for d in r.json()["folders"]] == ["x"]
    assert r.json()["files"] == []


def test_upload_with_path_lands_in_folder(server):
    r = httpx.post(f"{server}/files?path=/projects/x", headers=hdr("agt_alice"),
                   files={"file": ("report.txt", b"hello", "text/plain")})
    assert r.status_code == 201
    fid = r.json()["id"]
    r = httpx.get(f"{server}/files?path=/projects/x", headers=hdr("agt_alice"))
    assert fid in [f["id"] for f in r.json()["files"]]
    # bytes round-trip through the (in-memory) storage backend
    r = httpx.get(f"{server}/files/{fid}/download", headers=hdr("agt_alice"))
    assert r.status_code == 200 and r.content == b"hello"


def test_upload_default_path_is_home(server):
    r = httpx.post(f"{server}/files", headers=hdr("agt_bob"),
                   files={"file": ("notes.txt", b"n", "text/plain")})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    r = httpx.get(f"{server}/files?path=/home/bob", headers=hdr("agt_bob"))
    assert fid in [f["id"] for f in r.json()["files"]]
    d = httpx.get(f"{server}/files?path=/", headers=hdr("agt_bob"))
    assert "home" in [x["name"] for x in d.json()["folders"]]


def test_traversal_and_charset_guards(server):
    for bad in ("/a/../b", "/..", "relative", "/a/ /b!"):
        r = httpx.post(f"{server}/folders", json={"path": bad},
                       headers=hdr("agt_alice"))
        assert r.status_code == 400, (bad, r.status_code)
        r = httpx.get(f"{server}/files", params={"path": bad},
                      headers=hdr("agt_alice"))
        assert r.status_code == 400, (bad, r.status_code)
        r = httpx.post(f"{server}/files", params={"path": bad},
                       headers=hdr("agt_alice"),
                       files={"file": ("ok.txt", b"x", "text/plain")})
        assert r.status_code == 400, (bad, r.status_code)
    # a file NAME violating the segment charset is refused too
    r = httpx.post(f"{server}/files?path=/projects/x", headers=hdr("agt_alice"),
                   files={"file": ("bad!.txt", b"x", "text/plain")})
    assert r.status_code == 400


def test_subtree_write_rule_peer_denied(server):
    # bob can see nothing in alice's tree and can not write there
    r = httpx.post(f"{server}/files?path=/projects/x", headers=hdr("agt_bob"),
                   files={"file": ("intruder.txt", b"x", "text/plain")})
    assert r.status_code == 403
    r = httpx.post(f"{server}/folders", json={"path": "/projects/x/z"},
                   headers=hdr("agt_bob"))
    assert r.status_code == 403
    # and he can not delete alice's file either
    fid = httpx.get(f"{server}/files?path=/projects/x",
                    headers=hdr("agt_alice")).json()["files"][0]["id"]
    r = httpx.delete(f"{server}/files/{fid}", headers=hdr("agt_bob"))
    assert r.status_code == 403
    # personal namespaces are reserved: nobody creates folders in bob's home
    r = httpx.post(f"{server}/folders", json={"path": "/home/bob/junk"},
                   headers=hdr("agt_alice"))
    assert r.status_code == 403


def test_empty_folder_delete_ok_nonempty_409(server):
    r = httpx.post(f"{server}/folders", json={"path": "/projects/empty/deep"},
                   headers=hdr("agt_alice"))
    assert r.status_code == 201
    # /projects/empty still holds `deep` -> not empty
    r = httpx.delete(f"{server}/folders/projects/empty", headers=hdr("agt_alice"))
    assert r.status_code == 409
    r = httpx.delete(f"{server}/folders/projects/empty/deep",
                     headers=hdr("agt_alice"))
    assert r.status_code == 200 and r.json() == {"deleted": "/projects/empty/deep"}
    r = httpx.delete(f"{server}/folders/projects/empty", headers=hdr("agt_alice"))
    assert r.status_code == 200
    # non-empty /projects/x -> 409; unknown folder -> 404; stranger -> 403
    r = httpx.delete(f"{server}/folders/projects/x", headers=hdr("agt_alice"))
    assert r.status_code == 409
    r = httpx.delete(f"{server}/folders/projects/nope", headers=hdr("agt_alice"))
    assert r.status_code == 404
    r = httpx.delete(f"{server}/folders/projects/x", headers=hdr("agt_bob"))
    assert r.status_code == 403
    # root may delete anywhere (the tree owner may not be blocked by root-only ops)
    r = httpx.post(f"{server}/folders", json={"path": "/projects/tmp"},
                   headers=hdr("agt_alice"))
    r = httpx.delete(f"{server}/folders/projects/tmp", headers=hdr("hum_root"))
    assert r.status_code == 200


def test_listing_own_scope_only(server):
    # R40: bob's listings never leak alice's tree; the root human's listing
    # is own-scope too (named reads are the reach-down mechanism)
    r = httpx.get(f"{server}/files?path=/projects/x", headers=hdr("agt_bob"))
    assert r.json() == {"folders": [], "files": []}
    r = httpx.get(f"{server}/files?path=/projects/x", headers=hdr("hum_root"))
    assert r.json() == {"folders": [], "files": []}
