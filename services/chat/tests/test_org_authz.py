"""R62 sec-01 org-authorization tests — run against a live mongod + app.

Every request is real HTTP. Requires a FRESH database: the first test
exercises the first-actor bootstrap, which only exists while the actors
collection is empty (module runs tests in definition order).

Usage:
  TG_MONGO_URL='mongodb://127.0.0.1:27018/treegent?directConnection=true' \
    TG_SERVICE_TOKEN=dev-service-token \
    uv run uvicorn services.chat.app.main:app --host 127.0.0.1 --port 8100 &
  TG_TEST_BASE=http://127.0.0.1:8100 uv run pytest services/chat/tests/test_org_authz.py -v
"""
import os

import httpx
import pytest

BASE = os.environ.get("TG_TEST_BASE", "http://127.0.0.1:8100")
TOK = os.environ.get("TG_TEST_TOKEN", "dev-service-token")

W: dict = {}   # seeded world: rootuser (root human), peer1/peer2 (agents),
               # clerk (non-root human), agent keys, channel + inbox row


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=10) as c:
        yield c


def hdr(actor_id):
    """Central tier: trusted same-host caller acting FOR that actor."""
    return {"X-Service-Token": TOK, "X-Actor-Id": actor_id}


def key_hdr(key):
    """Agent tier: the only credential an agent host holds."""
    return {"X-Agent-Key": key}


def snapshot(client):
    """Full state a rejected org mutation must NOT change: org tree,
    conversation membership, inbox data."""
    actors = client.get("/actors", headers=hdr(W["rootuser"])).json()
    org_state = {
        a["id"]: (a["username"], a["kind"], a["org"]["parent_id"],
                  tuple(a["org"]["ancestors"]), a["org"]["depth"])
        for a in actors
    }
    conv = client.get(f"/conversations/{W['conv']}", headers=hdr(W["rootuser"])).json()
    inbox = client.get("/inbox", headers=hdr(W["peer1"])).json()
    return {
        "org": org_state,
        "conv_members": sorted(conv["members"]),
        "inbox_rows": [(r.get("message_id"), r.get("body"),
                        r.get("delivered_at")) for r in inbox],
    }


def denied(client, call):
    """Run one mutation request; expect 403 and ZERO state change."""
    before = snapshot(client)
    r = call()
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    after = snapshot(client)
    assert after == before, "rejected mutation changed org/conversation/inbox state"
    return r


# --- 5. bootstrap (must run first on a fresh DB) ------------------------

def test_bootstrap_first_actor_allowed_then_closed(client):
    """Empty actors collection -> central `boot` pseudo-actor creates the
    first actor. Once any actor exists, the exception is closed."""
    r = client.post("/actors", json={
        "username": "rootuser", "display_name": "Root Human", "kind": "human"},
        headers=hdr("boot"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["org"] == {"parent_id": None, "ancestors": [], "depth": 0}
    W["rootuser"] = body["id"]
    # exception is narrow: a SECOND create via boot is refused
    r = client.post("/actors", json={
        "username": "sneak", "display_name": "Sneak", "kind": "human"},
        headers=hdr("boot"))
    assert r.status_code == 403, r.text
    names = [a["username"] for a in client.get("/actors", headers=hdr(W["rootuser"])).json()]
    assert "sneak" not in names


# --- 1. seed ------------------------------------------------------------

@pytest.fixture(scope="module")
def world(client):
    assert W.get("rootuser"), "fresh DB required: bootstrap test must run first"
    rootuser = W["rootuser"]
    for uname, kind in (("peer1", "agent"), ("peer2", "agent"),
                        ("clerk", "human")):
        r = client.post("/actors", json={
            "username": uname, "display_name": uname.title(),
            "kind": kind, "parent_id": rootuser}, headers=hdr(rootuser))
        assert r.status_code == 201, r.text
        W[uname] = r.json()["id"]
    for uname in ("peer1", "peer2"):
        r = client.post(f"/internal/agents/{W[uname]}/keys", headers=hdr(rootuser))
        assert r.status_code == 200, r.text
        W[uname + "_key"] = r.json()["key"]
    # conversation + inbox row so rejected deletes can be side-effect checked
    r = client.post("/conversations", json={
        "kind": "channel", "name": "ops",
        "member_ids": [W["peer1"], W["peer2"]]}, headers=hdr(rootuser))
    assert r.status_code == 201, r.text
    W["conv"] = r.json()["id"]
    r = client.post(f"/conversations/{W['conv']}/messages",
                    json={"body": "hello peer1"}, headers=hdr(rootuser))
    assert r.status_code == 201, r.text
    assert W["rootuser"] == client.get("/actors/me", headers=hdr(rootuser)).json()["id"]
    assert client.get("/actors/me", headers=hdr(rootuser)).json()["org"]["depth"] == 0
    return W


# --- 2. agent key (peer2) — all denied ---------------------------------

def test_agent_key_cannot_create_actor(client, world):
    denied(client, lambda: client.post("/actors", json={
        "username": "intruder", "display_name": "Intruder",
        "kind": "human"}, headers=key_hdr(W["peer2_key"])))
    denied(client, lambda: client.post("/actors", json={
        "username": "intruder2", "display_name": "Intruder2", "kind": "agent",
        "parent_id": W["peer2"]}, headers=key_hdr(W["peer2_key"])))


def test_agent_key_cannot_reparent_peer_under_itself(client, world):
    denied(client, lambda: client.put(
        f"/actors/{W['peer1']}/parent", json={"parent_id": W["peer2"]},
        headers=key_hdr(W["peer2_key"])))


def test_agent_key_cannot_delete_peer(client, world):
    denied(client, lambda: client.delete(
        f"/actors/{W['peer1']}", headers=key_hdr(W["peer2_key"])))
    assert client.get(f"/actors/{W['peer1']}",
                      headers=hdr(W["rootuser"])).status_code == 200


def test_agent_key_cannot_reparent_itself(client, world):
    # promote itself to root level
    denied(client, lambda: client.put(
        f"/actors/{W['peer2']}/parent", json={"parent_id": None},
        headers=key_hdr(W["peer2_key"])))
    # nor tuck itself under a peer
    denied(client, lambda: client.put(
        f"/actors/{W['peer2']}/parent", json={"parent_id": W["peer1"]},
        headers=key_hdr(W["peer2_key"])))


# --- 3. non-root human (clerk, depth 1) — same denials ------------------

def test_nonroot_human_cannot_mutate_org(client, world):
    c = W["clerk"]
    denied(client, lambda: client.post("/actors", json={
        "username": "rootless", "display_name": "Rootless", "kind": "human"},
        headers=hdr(c)))
    denied(client, lambda: client.put(
        f"/actors/{W['peer1']}/parent", json={"parent_id": c},
        headers=hdr(c)))
    denied(client, lambda: client.delete(f"/actors/{W['peer1']}", headers=hdr(c)))
    denied(client, lambda: client.put(
        f"/actors/{c}/parent", json={"parent_id": None}, headers=hdr(c)))


# --- 4. root human (rootuser) via central tier — mutations succeed ----------

def test_root_human_can_mutate_org(client, world):
    rootuser = W["rootuser"]
    r = client.post("/actors", json={
        "username": "nina", "display_name": "Nina", "kind": "agent",
        "parent_id": rootuser}, headers=hdr(rootuser))
    assert r.status_code == 201, r.text
    nina = r.json()["id"]
    # reparent peer1 under peer2 (root may restructure freely)
    r = client.put(f"/actors/{W['peer1']}/parent",
                   json={"parent_id": W["peer2"]}, headers=hdr(rootuser))
    assert r.status_code == 200, r.text
    peer1 = client.get(f"/actors/{W['peer1']}", headers=hdr(rootuser)).json()
    assert peer1["org"]["ancestors"] == [rootuser, W["peer2"]]
    # restore
    r = client.put(f"/actors/{W['peer1']}/parent",
                   json={"parent_id": rootuser}, headers=hdr(rootuser))
    assert r.status_code == 200, r.text
    assert client.get(f"/actors/{W['peer1']}",
                      headers=hdr(rootuser)).json()["org"]["ancestors"] == [rootuser]
    # delete the actor it created
    r = client.delete(f"/actors/{nina}", headers=hdr(rootuser))
    assert r.status_code == 200, r.text
    assert client.get(f"/actors/{nina}",
                      headers=hdr(rootuser)).status_code == 404


# --- 6. rejected reparent leaves peer secret isolation intact ------------

def test_rejected_reparent_keeps_secret_isolation(client, world):
    """The secrets service derives reach-down from org.ancestors. After a
    REJECTED attempt to move peer1 under peer2, peer2 must still not be
    in peer1's ancestors — the peer's secrets stay denied to peer2."""
    denied(client, lambda: client.put(
        f"/actors/{W['peer1']}/parent", json={"parent_id": W["peer2"]},
        headers=key_hdr(W["peer2_key"])))
    peer1 = client.get(f"/actors/{W['peer1']}", headers=hdr(W["rootuser"])).json()
    assert peer1["org"]["ancestors"] == [W["rootuser"]]
    assert peer1["org"]["parent_id"] == W["rootuser"]
    assert W["peer2"] not in peer1["org"]["ancestors"]


# --- 7. conversation membership policy (issue #22) ----------------------

def test_create_conv_members_must_be_in_creator_subtree(client, world):
    """Issue #22 regression: an agent key could open a channel with every
    human and read everything silently. Members must now lie inside the
    creator's org subtree (root is exempt: the whole tree is their
    subtree)."""
    # the report's exploit, verbatim: agent key + channel with all humans
    denied(client, lambda: client.post("/conversations", json={
        "kind": "channel", "name": "standup",
        "member_ids": [W["rootuser"], W["clerk"], W["peer1"]]},
        headers=key_hdr(W["peer2_key"])))
    names = [c["name"] for c in
             client.get("/conversations", headers=hdr(W["rootuser"])).json()]
    assert "standup" not in names
    # same policy for a non-root human without descendants
    denied(client, lambda: client.post("/conversations", json={
        "kind": "channel", "name": "clerk-grab", "member_ids": [W["peer2"]]},
        headers=hdr(W["clerk"])))
    # positive: a creator may include its own subtree
    r = client.post("/actors", json={
        "username": "sub1", "display_name": "Sub One", "kind": "agent",
        "parent_id": W["peer1"]}, headers=hdr(W["rootuser"]))
    assert r.status_code == 201, r.text
    W["sub1"] = r.json()["id"]
    r = client.post("/conversations", json={
        "kind": "channel", "name": "team1", "member_ids": [W["sub1"]]},
        headers=key_hdr(W["peer1_key"]))
    assert r.status_code == 201, r.text
    assert sorted(r.json()["members"]) == sorted([W["peer1"], W["sub1"]])
    W["team1"] = r.json()["id"]
    # ...but the subtree never stretches to peers
    denied(client, lambda: client.post("/conversations", json={
        "kind": "channel", "name": "team2",
        "member_ids": [W["sub1"], W["peer2"]]},
        headers=key_hdr(W["peer1_key"])))
    # root keeps picking anyone (company channels stay possible)
    r = client.post("/conversations", json={
        "kind": "channel", "name": "company",
        "member_ids": [W["clerk"], W["peer1"], W["peer2"]]},
        headers=hdr(W["rootuser"]))
    assert r.status_code == 201, r.text
    assert sorted(r.json()["members"]) == sorted(
        [W["rootuser"], W["clerk"], W["peer1"], W["peer2"]])


def test_add_member_only_creator_or_root(client, world):
    """Issue #22 regression: any member could add ANYONE to a channel
    (agent keys included). Adding third parties is now creator/root-only,
    bounded by the creator's subtree; a plain member can only re-add
    itself and a non-member cannot join at all."""
    conv = W["conv"]   # created by rootuser; members rootuser/peer1/peer2
    # plain member may not pull a third party in (header or agent key)
    denied(client, lambda: client.post(f"/conversations/{conv}/members",
        json={"member_ids": [W["clerk"]]}, headers=hdr(W["peer1"])))
    denied(client, lambda: client.post(f"/conversations/{conv}/members",
        json={"member_ids": [W["clerk"]]}, headers=key_hdr(W["peer1_key"])))
    # a non-member cannot self-join — existing private chats stay closed
    denied(client, lambda: client.post(f"/conversations/{conv}/members",
        json={"member_ids": [W["clerk"]]}, headers=hdr(W["clerk"])))
    # a member of team1 (sub1, non-creator) cannot pull peers in either
    denied(client, lambda: client.post(f"/conversations/{W['team1']}/members",
        json={"member_ids": [W["peer2"]]}, headers=hdr(W["sub1"])))
    # self re-add is a harmless no-op for members
    r = client.post(f"/conversations/{conv}/members",
                    json={"member_ids": [W["peer1"]]}, headers=hdr(W["peer1"]))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["members"]) == sorted(
        [W["rootuser"], W["peer1"], W["peer2"]])
    # a non-root creator is still bounded by its subtree when adding
    denied(client, lambda: client.post(f"/conversations/{W['team1']}/members",
        json={"member_ids": [W["peer2"]]}, headers=key_hdr(W["peer1_key"])))
    # root may add a third party anywhere (moderation)
    r = client.post(f"/conversations/{W['team1']}/members",
                    json={"member_ids": [W["clerk"]]}, headers=hdr(W["rootuser"]))
    assert r.status_code == 200, r.text
    assert W["clerk"] in r.json()["members"]
    # restore
    r = client.delete(f"/conversations/{W['team1']}/members/{W['clerk']}",
                      headers=hdr(W["rootuser"]))
    assert r.status_code == 200, r.text
    assert W["clerk"] not in r.json()["members"]


def test_remove_member_only_self_creator_or_root(client, world):
    """Issue #22 regression: any member could remove ANYONE from a
    channel. Leaving is self-only; third-party removal is creator/root
    moderation."""
    conv = W["conv"]
    # plain member may not pull a third party out (header or agent key)
    denied(client, lambda: client.delete(
        f"/conversations/{conv}/members/{W['peer2']}", headers=hdr(W["peer1"])))
    denied(client, lambda: client.delete(
        f"/conversations/{conv}/members/{W['peer2']}",
        headers=key_hdr(W["peer1_key"])))
    # leaving on one's own is fine
    r = client.delete(f"/conversations/{conv}/members/{W['peer2']}",
                      headers=key_hdr(W["peer2_key"]))
    assert r.status_code == 200, r.text
    assert W["peer2"] not in r.json()["members"]
    # creator (root here) puts things back
    r = client.post(f"/conversations/{conv}/members",
                    json={"member_ids": [W["peer2"]]}, headers=hdr(W["rootuser"]))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["members"]) == sorted(
        [W["rootuser"], W["peer1"], W["peer2"]])
    # a non-root creator moderates its own team channel
    r = client.delete(
        f"/conversations/{W['team1']}/members/{W['sub1']}",
        headers=key_hdr(W["peer1_key"]))
    assert r.status_code == 200, r.text
    assert W["sub1"] not in r.json()["members"]
    # restore (creator re-adds within its subtree)
    r = client.post(f"/conversations/{W['team1']}/members",
                    json={"member_ids": [W["sub1"]]}, headers=key_hdr(W["peer1_key"]))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["members"]) == sorted([W["peer1"], W["sub1"]])
