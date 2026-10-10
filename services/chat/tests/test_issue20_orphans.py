"""Issue #20 regression: actor delete/reparent must not orphan mail-service
approvals.

Before this fix: delete_actor scrubbed conversations/inbox/agent_keys/
wake_events but never the `approvals` rows naming the actor in EITHER
role (requester_id / approver_id) — the rows stayed pending with a
vanished approver, hidden and un-decidable. org.move recomputed org
paths but never the pending approvals, which snapshot the requester's
then-superior as approver — the stale ex-approver kept deciding rights
after a reparent.

Suite runs a REAL uvicorn subprocess (the prod shape) against a
DISPOSABLE Mongo — same harness as services/proxy/tests/test_jobs_auth.py.
"""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TG_TEST_MONGO_URL"),
    reason="TG_TEST_MONGO_URL not set — point it at a disposable Mongo")

PORT = 18099
BASE = f"http://127.0.0.1:{PORT}"
TOK = "test-token"


def hdr(actor_id):
    """Central tier: trusted same-host caller acting FOR that actor."""
    return {"X-Service-Token": TOK, "X-Actor-Id": actor_id}


def now():
    return datetime.now(timezone.utc)


COLS = ("actors", "approvals", "mail_messages", "mailboxes", "permissions",
        "wake_events", "agent_keys", "conversations", "inbox", "messages")


def _wipe():
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    for col in COLS:
        db[col].delete_many({})
    mc.close()


def _wipe_and_seed():
    import pymongo
    _wipe()
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]

    def agent(aid, uname, parent):
        return {"_id": aid, "username": uname, "display_name": uname,
                "kind": "agent",
                "org": {"parent_id": parent,
                        "ancestors": ["hum_root"] + ([parent] if parent != "hum_root" else []),
                        "depth": 2 if parent != "hum_root" else 1}}

    db.actors.insert_many([
        {"_id": "hum_root", "username": "root", "display_name": "Root",
         "kind": "human",
         "org": {"parent_id": None, "ancestors": [], "depth": 0}},
        agent("agt_m1", "mone", "hum_root"),   # old superior
        agent("agt_m2", "mtwo", "hum_root"),   # new superior
        agent("agt_r", "req", "agt_m1"),       # reparented requester
        agent("agt_del1", "delone", "hum_root"),   # deleted requester
        agent("agt_del2", "deltwo", "hum_root"),   # deleted approver
        agent("agt_keep", "keep", "hum_root"),     # survivor
    ])
    db.mailboxes.insert_many([
        {"_id": "mbx_del1", "address": "delone@treegent.local",
         "kind": "personal", "owner": "agt_del1"},
        {"_id": "mbx_keep", "address": "keep@treegent.local",
         "kind": "personal", "owner": "agt_keep"},
        {"_id": "mbx_shared", "address": "shared@treegent.local",
         "kind": "shared", "members": ["agt_del1", "agt_keep"]},
    ])
    db.mail_messages.insert_many([
        {"_id": "mail_p1", "mailbox_id": "mbx_del1", "direction": "in",
         "from_addr": "a@example.invalid", "to": "delone@treegent.local",
         "subject": "s", "text": "hi", "status": "received", "ts": now()},
        {"_id": "mail_s1", "mailbox_id": "mbx_shared", "direction": "out",
         "from_addr": "shared@treegent.local", "to": "x@example.invalid",
         "subject": "s", "text": "hi", "status": "pending",
         "approval_id": "apr_del1", "ts": now()},
        {"_id": "mail_k1", "mailbox_id": "mbx_keep", "direction": "out",
         "from_addr": "keep@treegent.local", "to": "x@example.invalid",
         "subject": "s", "text": "hi", "status": "pending",
         "approval_id": "apr_del2", "ts": now()},
        {"_id": "mail_move", "mailbox_id": "mbx_keep", "direction": "out",
         "from_addr": "keep@treegent.local", "to": "x@example.invalid",
         "subject": "s", "text": "hi", "status": "pending",
         "approval_id": "apr_move", "ts": now()},
    ])
    for apr, req, appr, status in (
            ("apr_move", "agt_r", "agt_m1", "pending"),
            ("apr_done", "agt_r", "agt_m1", "approved"),
            ("apr_del1", "agt_del1", "agt_m1", "pending"),
            ("apr_del2", "agt_keep", "agt_del2", "pending")):
        db.approvals.insert_one({
            "_id": apr, "action": "mail.send",
            "payload": {"mail_id": "mail_move" if apr == "apr_move" else
                        {"apr_del1": "mail_s1", "apr_del2": "mail_k1",
                         "apr_done": "mail_p1"}[apr],
                        "from": "x@example.invalid", "to": "y@example.invalid",
                        "subject": "s"},
            "requester_id": req, "approver_id": appr,
            "status": status, "mode": "background", "created_at": now()})
    db.wake_events.insert_many([
        {"_id": "wke_old", "agent_id": "agt_m1", "reason": "approval",
         "approval_id": "apr_move", "created_at": now(), "consumed": False},
        {"_id": "wke_d1", "agent_id": "agt_m1", "reason": "approval",
         "approval_id": "apr_del1", "created_at": now(), "consumed": False},
        {"_id": "wke_d2", "agent_id": "agt_keep", "reason": "approval",
         "approval_id": "apr_del2", "created_at": now(), "consumed": False},
    ])
    mc.close()


@pytest.fixture(scope="module")
def server():
    _wipe_and_seed()
    env = dict(os.environ,
               TG_MONGO_URL=os.environ["TG_TEST_MONGO_URL"],
               TG_SERVICE_TOKEN=TOK)
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "services.chat.app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=os.path.join(os.path.dirname(__file__), "..", "..", ".."),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("chat test server did not come up")
        yield
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=10)


def _db():
    import pymongo
    return pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])["treegent"]


def test_reparent_reassigns_pending_approvals_and_wakes(server):
    """org.move must move the moved actor's PENDING approvals to its new
    superior (the approver is a send-time snapshot) — decided rows are
    history and stay put."""
    r = httpx.put(f"{BASE}/actors/agt_r/parent", json={"parent_id": "agt_m2"},
                  headers=hdr("hum_root"), timeout=10)
    assert r.status_code == 200, r.text
    db = _db()
    moved = db.approvals.find_one({"_id": "apr_move"})
    assert moved and moved["approver_id"] == "agt_m2", moved
    done = db.approvals.find_one({"_id": "apr_done"})
    assert done and done["approver_id"] == "agt_m1", done
    # stale approval wake for the ex-approver is dead, the new approver
    # (an agent) is woken
    stale = list(db.wake_events.find(
        {"approval_id": "apr_move", "agent_id": "agt_m1", "consumed": False}))
    assert stale == [], stale
    fresh = list(db.wake_events.find(
        {"approval_id": "apr_move", "agent_id": "agt_m2", "consumed": False}))
    assert len(fresh) == 1, fresh


def test_delete_requester_scrubs_approvals_and_mail(server):
    """Deleting the requester removes its approval rows in the requester
    role, settles the pending mail behind them, drops their wakes, and
    clears its personal mailbox while shared boxes survive."""
    r = httpx.delete(f"{BASE}/actors/agt_del1", headers=hdr("hum_root"),
                     timeout=10)
    assert r.status_code == 200, r.text
    db = _db()
    rows = list(db.approvals.find(
        {"$or": [{"requester_id": "agt_del1"}, {"approver_id": "agt_del1"}]}))
    assert rows == [], rows
    wakes = list(db.wake_events.find({"approval_id": "apr_del1"}))
    assert wakes == [], wakes
    # the pending shared-box mail behind the removed approval is dead
    s1 = db.mail_messages.find_one({"_id": "mail_s1"})
    assert s1 and s1["status"] == "failed", s1
    # personal box dies with its owner, its mail goes too
    assert db.mailboxes.find_one({"_id": "mbx_del1"}) is None
    assert db.mail_messages.find_one({"_id": "mail_p1"}) is None
    # shared box keeps existing, membership pulled (mirrors conversations)
    shared = db.mailboxes.find_one({"_id": "mbx_shared"})
    assert shared and shared.get("members") == ["agt_keep"], shared
    assert db.mail_messages.find_one({"_id": "mail_s1"}) is not None


def test_delete_approver_scrubs_and_settles_pending_mail(server):
    """Deleting the approver removes its approval rows in the approver
    role (the issue's shape 1: nobody could see or decide them), settles
    the still-pending mail behind them and drops the dead wakes."""
    r = httpx.delete(f"{BASE}/actors/agt_del2", headers=hdr("hum_root"),
                     timeout=10)
    assert r.status_code == 200, r.text
    db = _db()
    rows = list(db.approvals.find(
        {"$or": [{"requester_id": "agt_del2"}, {"approver_id": "agt_del2"}]}))
    assert rows == [], rows
    wakes = list(db.wake_events.find({"approval_id": "apr_del2"}))
    assert wakes == [], wakes
    k1 = db.mail_messages.find_one({"_id": "mail_k1"})
    assert k1 and k1["status"] == "failed", k1
    # the survivor's personal mailbox is untouched
    assert db.mailboxes.find_one({"_id": "mbx_keep"}) is not None


def test_unrelated_approvals_survive_cleanup(server):
    """Over-deletion guard: rows not naming the deleted actors stay."""
    db = _db()
    ids = sorted(a["_id"] for a in db.approvals.find({}))
    assert ids == ["apr_done", "apr_move"], ids
