"""Issues #21 + #25 — chat inbox fanout + id-scoped ack contract.

Run against a live mongod + app (like test_api.py):
  deploy/run-mongo.sh
  TG_DEV=1 uv run uvicorn services.chat.app.main:app --port 8000 &
  cd services/chat && TG_TEST_BASE=http://127.0.0.1:8000 uv run pytest tests/

#25: /internal/agent-inbox used to emit message_id/sender/body/received_at
but NOT the inbox row _id — the runtime's ack ids came back empty and
agent-inbox-delivered fell back to a BLANKET ack, marking messages that
arrived mid-turn delivered without ever being injected (silent loss).

#21: _dispatch wrote an inbox row for EVERY conversation member while only
the wake set (DMs all / channels @-mentioned) is ever notified — a
100-member channel broadcast queued 98 rows nobody was told about, which
starved DM/mention rows out of the oldest-first pending pages.
"""
import os

import httpx

BASE = os.environ.get("TG_TEST_BASE", "http://127.0.0.1:8000")
TOK = os.environ.get("TG_TEST_TOKEN", "dev-service-token")

import pytest  # noqa: E402

W: dict = {}


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=10) as c:
        yield c


def hdr(actor_id):
    return {"X-Service-Token": TOK, "X-Actor-Id": actor_id}


def svc_hdr():
    """Service-only endpoints (internal tier): token, no actor."""
    return {"X-Service-Token": TOK}


def inbox_rows(client, actor_id):
    r = client.get("/inbox", headers=hdr(actor_id))
    assert r.status_code == 200, r.text
    return r.json()


def rows_for_message(client, actor_id, message_id):
    return [row for row in inbox_rows(client, actor_id)
            if row["message_id"] == message_id]


@pytest.fixture(scope="module")
def world(client):
    w = {}
    r = client.post("/actors", json={
        "username": "ceo", "display_name": "CEO", "kind": "human"},
        headers=hdr("boot"))
    assert r.status_code == 201, r.text
    w["ceo"] = r.json()["id"]
    for uname in ("worker", "second", "quiet"):
        r = client.post("/actors", json={
            "username": uname, "display_name": uname.title(), "kind": "agent",
            "parent_id": w["ceo"]}, headers=hdr(w["ceo"]))
        assert r.status_code == 201, r.text
        w[uname] = r.json()["id"]
    return w


def test_agent_inbox_emits_inbox_row_id(client, world):
    """#25: the pending payload must carry the inbox row _id (inbox_id) —
    the id agent-inbox-delivered matches on. message_id is a DIFFERENT id
    space (the message doc): acking with it would match zero rows."""
    r = client.post(f"/conversations/dm/{world['worker']}",
                    headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    W["dm"] = r.json()["id"]
    r = client.post(f"/conversations/{W['dm']}/messages",
                    json={"body": "ping one"}, headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    msg_id = r.json()["id"]
    W["msg_ping_one"] = msg_id

    r = client.get("/internal/agent-inbox",
                   params={"agent_id": world["worker"]}, headers=svc_hdr())
    assert r.status_code == 200, r.text
    ms = [m for m in r.json()["messages"] if m.get("body") == "ping one"]
    assert ms, r.json()
    m = ms[0]
    assert m.get("inbox_id"), \
        "agent-inbox must emit the inbox row id (issue #25 — without it " \
        "the runtime records no ack ids and the ack blanket-marks rows)"
    assert m["message_id"] == msg_id
    assert m["inbox_id"] != m["message_id"], \
        "inbox_id must be the INBOX ROW id, distinct from message_id"
    row_ids = {row["id"] for row in inbox_rows(client, world["worker"])}
    assert m["inbox_id"] in row_ids, \
        "inbox_id must match a real inbox row _id (the delivered " \
        "endpoint's query space)"
    W["row_ping_one"] = m["inbox_id"]


def test_id_scoped_ack_preserves_midturn_rows(client, world):
    """#25 loss scenario end to end: a message arriving between the
    runtime's read and its ack must stay pending. The ack ids are collected
    exactly like loop.py does (m.get('inbox_id') or m.get('_id'))."""
    r = client.get("/internal/agent-inbox",
                   params={"agent_id": world["worker"]}, headers=svc_hdr())
    ids = [m.get("inbox_id") or m.get("_id")
           for m in r.json()["messages"]]
    ids = [i for i in ids if i]
    assert ids, \
        "runtime-collected ack ids must be non-empty (issue #25 — empty " \
        "ids made the ack blanket-mark every undelivered row)"
    # message arrives MID-TURN, after the read above
    r = client.post(f"/conversations/{W['dm']}/messages",
                    json={"body": "ping two"}, headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    W["msg_ping_two"] = r.json()["id"]

    r = client.post("/internal/agent-inbox-delivered",
                    json={"agent_id": world["worker"], "inbox_ids": ids},
                    headers=svc_hdr())
    assert r.status_code == 200, r.text

    # the acked row is delivered (no longer pending)...
    assert rows_for_message(client, world["worker"], W["msg_ping_one"]) == []
    # ...but the mid-turn row must STILL be pending (it was never injected)
    r = client.get("/internal/agent-inbox",
                   params={"agent_id": world["worker"]}, headers=svc_hdr())
    bodies = [m.get("body") for m in r.json()["messages"]]
    assert "ping two" in bodies, \
        "mid-turn message was acked without injection (silent loss, #25)"
    assert rows_for_message(client, world["worker"], W["msg_ping_two"]), \
        "mid-turn inbox row must stay undelivered"


def test_channel_broadcast_rows_only_for_wake_set(client, world):
    """#21: inbox rows go to the wake set only — a channel broadcast writes
    NO rows; @mentions write rows exactly for the mentioned agents."""
    r = client.post("/conversations", json={
        "kind": "channel", "name": "general",
        "member_ids": [world["worker"], world["second"], world["quiet"]]},
        headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    ch = r.json()["id"]

    # broadcast, no mention -> wakes 0, rows 0 (was: 3 rows, one per member)
    r = client.post(f"/conversations/{ch}/messages",
                    json={"body": "standup notes, no action needed"},
                    headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    assert r.json()["wakes_created"] == 0
    m1 = r.json()["id"]
    for uname in ("worker", "second", "quiet"):
        assert rows_for_message(client, world[uname], m1) == [], \
            f"broadcast wrote an inbox row for non-mentioned {uname} (#21)"

    # single mention -> exactly ONE row, for the mentioned agent
    r = client.post(f"/conversations/{ch}/messages",
                    json={"body": "please review @worker"},
                    headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    assert r.json()["wakes_created"] == 1
    m2 = r.json()["id"]
    assert len(rows_for_message(client, world["worker"], m2)) == 1
    assert rows_for_message(client, world["second"], m2) == []
    assert rows_for_message(client, world["quiet"], m2) == []

    # double mention -> exactly two rows
    r = client.post(f"/conversations/{ch}/messages",
                    json={"body": "pair up @worker and @second"},
                    headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    assert r.json()["wakes_created"] == 2
    m3 = r.json()["id"]
    assert len(rows_for_message(client, world["worker"], m3)) == 1
    assert len(rows_for_message(client, world["second"], m3)) == 1
    assert rows_for_message(client, world["quiet"], m3) == []


def test_dm_rows_reach_all_non_sender_members(client, world):
    """#21 guard: the DM wake set is ALL members — rows must keep flowing
    there (only the channel broadcast fanout narrows)."""
    r = client.post(f"/conversations/{W['dm']}/messages",
                    json={"body": "dm row check"}, headers=hdr(world["ceo"]))
    assert r.status_code == 201, r.text
    m = r.json()["id"]
    assert len(rows_for_message(client, world["worker"], m)) == 1
    assert rows_for_message(client, world["ceo"], m) == [], \
        "sender must never get an inbox row"
