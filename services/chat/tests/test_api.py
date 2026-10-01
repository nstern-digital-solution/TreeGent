"""M1 API tests — run against a live mongod + app instance.

Usage:
  deploy/run-mongo.sh
  uv run uvicorn --factory services.chat.app.main:app --port 8000 &
  cd services/chat && TG_TEST_BASE=http://127.0.0.1:8000 uv run pytest tests/
"""
import os

import pytest

BASE = os.environ.get("TG_TEST_BASE", "http://127.0.0.1:8000")
TOK = os.environ.get("TG_TEST_TOKEN", "dev-service-token")

import httpx


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=10) as c:
        yield c


def hdr(actor_id):
    return {"X-Service-Token": TOK, "X-Actor-Id": actor_id}


@pytest.fixture(scope="module")
def world(client):
    """Bootstrap: ceo (human) → head (human) → worker (agent); second human peer.
    Only the FIRST actor is created via the `boot` pseudo-actor (valid only
    while the actors collection is empty); everything else runs as ceo."""
    w = {}
    r = client.post("/actors", json={
        "username": "ceo", "display_name": "CEO", "kind": "human"}, headers=hdr("boot"))
    assert r.status_code == 201, r.text
    w["ceo"] = r.json()["id"]
    r = client.post("/actors", json={
        "username": "head", "display_name": "Head of Ops", "kind": "human",
        "parent_id": w["ceo"]}, headers=hdr(w["ceo"]))
    assert r.status_code == 201, r.text
    w["head"] = r.json()["id"]
    r = client.post("/actors", json={
        "username": "worker", "display_name": "Worker Agent", "kind": "agent",
        "parent_id": w["head"]}, headers=hdr(w["ceo"]))
    assert r.status_code == 201, r.text
    w["worker"] = r.json()["id"]
    return w


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_org_ancestors(client, world):
    r = client.get(f"/actors/{world['worker']}/superiors", headers=hdr(world["ceo"]))
    assert r.status_code == 200
    sup = r.json()
    assert [s["username"] for s in sup] == ["head", "ceo"]  # nearest first


def test_org_reparent_subtree(client, world):
    # create a subordinate under worker, then move worker under ceo directly
    r = client.post("/actors", json={
        "username": "junior", "display_name": "Junior Agent", "kind": "agent",
        "parent_id": world["worker"]}, headers=hdr(world["ceo"]))
    assert r.status_code == 201
    junior = r.json()["id"]
    r = client.put(f"/actors/{world['worker']}/parent",
                   json={"parent_id": world["ceo"]}, headers=hdr(world["ceo"]))
    assert r.status_code == 200
    assert r.json()["result"]["subtree_updated"] >= 2
    # junior's ancestors must now be [ceo, worker]
    r = client.get(f"/actors/{junior}", headers=hdr(world["ceo"]))
    anc = r.json()["org"]["ancestors"]
    assert anc == [world["ceo"], world["worker"]]
    # cycle rejected
    r = client.put(f"/actors/{world['ceo']}/parent",
                   json={"parent_id": junior}, headers=hdr(world["ceo"]))
    assert r.status_code == 400
    # restore
    client.put(f"/actors/{world['worker']}/parent",
               json={"parent_id": world["head"]}, headers=hdr(world["ceo"]))


def test_dm_and_wake_policy(client, world):
    # DM: human → agent must create a wake event
    r = client.post(f"/conversations/dm/{world['worker']}", headers=hdr(world["ceo"]))
    assert r.status_code == 201
    dm = r.json()["id"]
    r = client.post(f"/conversations/{dm}/messages", json={"body": "hi worker"},
                    headers=hdr(world["ceo"]))
    assert r.status_code == 201
    assert r.json()["wakes_created"] == 1
    # inbox has one undelivered row for worker with the message body
    r = client.get("/inbox", headers=hdr(world["worker"]))
    rows = r.json()
    assert len(rows) >= 1
    assert rows[-1]["delivered_at"] is None
    assert rows[-1]["body"] == "hi worker"


def test_channel_mention_wakes_only_mentioned(client, world):
    r = client.post("/actors", json={
        "username": "second", "display_name": "Second Agent", "kind": "agent",
        "parent_id": world["head"]}, headers=hdr(world["ceo"]))
    second = r.json()["id"]
    r = client.post("/conversations", json={
        "kind": "channel", "name": "general",
        "member_ids": [world["ceo"], world["worker"], second]},
        headers=hdr(world["ceo"]))
    assert r.status_code == 201
    ch = r.json()["id"]
    # no mention → no wake
    r = client.post(f"/conversations/{ch}/messages",
                    json={"body": "just chatter"}, headers=hdr(world["ceo"]))
    assert r.json()["wakes_created"] == 0
    # mention @worker → exactly one wake (worker), not second
    r = client.post(f"/conversations/{ch}/messages",
                    json={"body": "ping @worker please"}, headers=hdr(world["ceo"]))
    assert r.json()["wakes_created"] == 1


def test_inbox_deliver(client, world):
    r = client.post("/inbox/deliver", headers=hdr(world["worker"]))
    assert r.status_code == 200
    assert r.json()["delivered"] >= 1
    r = client.get("/inbox", headers=hdr(world["worker"]))
    for row in r.json():
        assert row["delivered_at"] is not None


def test_auth_rejected(client, world):
    r = client.get("/actors", headers={"X-Service-Token": "wrong", "X-Actor-Id": world["ceo"]})
    assert r.status_code == 401
    r = client.get("/actors", headers=hdr("nobody"))
    assert r.status_code == 403
