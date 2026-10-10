"""Issue #20 regression: root must SEE and DECIDE an orphaned approval.

Actor delete/reparent used to leave `approvals` rows naming a vanished
approver — hidden in every scope by list_approvals' own-scope skip (root
is neither requester nor approver) and un-decidable even by the root
human (approvals.read/decide were allow:["own"]). The requester's mail
then sat pending forever.

The fix shape under test: RULE_SEEDS grant `root` to approvals.read AND
approvals.decide, list_approvals' skip goes through the rule engine, and
the root human's approval view is unscoped (an orphaned row matches NO
role filter and must surface in every scope to be rescued).

Suite runs a REAL uvicorn subprocess (the prod shape) against a
DISPOSABLE Mongo — same harness as services/proxy/tests/test_jobs_auth.py.
The DB is pre-seeded with the PRE-FIX rule rows (allow:["own"]) so the
boot-time upgrade path is exercised like a real upgraded deployment.
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

PORT = 18098
BASE = f"http://127.0.0.1:{PORT}"
TOK = "test-token"


def hdr(actor_id):
    """Central tier: trusted same-host caller acting FOR that actor."""
    return {"X-Service-Token": TOK, "X-Actor-Id": actor_id}


def now():
    return datetime.now(timezone.utc)


def _wipe_and_seed():
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    for col in ("actors", "approvals", "mail_messages", "mailboxes",
                "permissions", "wake_events", "mail_adapters", "agent_keys"):
        db[col].delete_many({})
    # PRE-FIX rule rows (a deployed box upgraded in place: $setOnInsert
    # seeding never rewrites rows, R60c) — boot must upgrade these.
    db.permissions.insert_many([
        {"_id": "approvals.read", "allow": ["own"]},
        {"_id": "approvals.decide", "allow": ["own"]},
    ])
    db.actors.insert_many([
        {"_id": "hum_root", "username": "root", "display_name": "Root",
         "kind": "human",
         "org": {"parent_id": None, "ancestors": [], "depth": 0}},
        {"_id": "agt_boss", "username": "boss", "display_name": "Boss",
         "kind": "agent",
         "org": {"parent_id": "hum_root", "ancestors": ["hum_root"],
                 "depth": 1}},
        {"_id": "agt_req", "username": "req", "display_name": "Req",
         "kind": "agent",
         "org": {"parent_id": "agt_boss",
                 "ancestors": ["hum_root", "agt_boss"], "depth": 2}},
        {"_id": "hum_clerk", "username": "clerk", "display_name": "Clerk",
         "kind": "human",
         "org": {"parent_id": "hum_root", "ancestors": ["hum_root"],
                 "depth": 1}},
    ])
    # NOTE: no mailbox row is seeded — mail startup auto-creates canonical
    # boxes for every agent (and its box-migration collides with a seeded
    # same-address box); the endpoints under test never resolve mailbox_id.
    for apr, mail, appr, status in (
            ("apr_ghost", "mail_ghost", "agt_vanished", "pending"),
            ("apr_ghost2", "mail_ghost2", "agt_vanished", "pending"),
            ("apr_ok", "mail_ok", "agt_boss", "pending"),
            ("apr_done", "mail_done", "agt_boss", "approved")):
        db.approvals.insert_one({
            "_id": apr, "action": "mail.send",
            "payload": {"mail_id": mail, "from": "req@treegent.local",
                        "to": "x@example.invalid", "subject": "s"},
            "requester_id": "agt_req", "approver_id": appr,
            "status": status, "mode": "background", "created_at": now()})
        db.mail_messages.insert_one({
            "_id": mail, "mailbox_id": "mbx_req", "direction": "out",
            "from_addr": "req@treegent.local", "to": "x@example.invalid",
            "subject": "s", "text": "hi",
            "status": "pending", "approval_id": apr, "ts": now()})
    mc.close()


@pytest.fixture(scope="module")
def server():
    _wipe_and_seed()
    env = dict(os.environ,
               TG_MONGO_URL=os.environ["TG_TEST_MONGO_URL"],
               TG_SERVICE_TOKEN=TOK)
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "services.mail.app.__main__:app",
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
            pytest.fail("mail test server did not come up")
        yield
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=10)


def test_rule_seeds_grant_root_read_and_decide():
    """The visibility/decision rules themselves must name root (issue #20):
    decide-only was INCOMPLETE — root still could not SEE the orphan."""
    from treegent_common.perms import RULE_SEEDS
    seeds = {r["_id"]: r["allow"] for r in RULE_SEEDS}
    assert "root" in seeds["approvals.read"], seeds["approvals.read"]
    assert "root" in seeds["approvals.decide"], seeds["approvals.decide"]


def test_boot_upgrades_preexisting_rule_rows(server):
    """$setOnInsert seeding never rewrites rows (R60c) — a deployed box
    keeps allow:["own"] forever unless boot upgrades it. The upgrade must
    be additive: ['own'] becomes ['own', 'root']."""
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    db = mc["treegent"]
    for perm in ("approvals.read", "approvals.decide"):
        row = db.permissions.find_one({"_id": perm})
        assert row is not None, perm
        assert sorted(row["allow"]) == ["own", "root"], (perm, row["allow"])
    mc.close()


def test_root_sees_orphan_in_every_scope(server):
    """The orphan (approver 'agt_vanished' does not exist) names no live
    role holder — root must still see it in EVERY scope to rescue it."""
    for scope in ("inbox", "requested", "all"):
        r = httpx.get(f"{BASE}/approvals", params={"scope": scope},
                      headers=hdr("hum_root"), timeout=10)
        assert r.status_code == 200, (scope, r.text)
        ids = [a["id"] for a in r.json()]
        assert "apr_ghost" in ids, f"orphan hidden from root in scope={scope}"
        assert "apr_ghost2" in ids, f"orphan hidden from root in scope={scope}"


def test_requester_scope_semantics_unchanged(server):
    """Non-root scoping stays tight: the requester sees its own rows in
    requested/all but not in inbox (it is not the approver), and can never
    decide its own send."""
    r = httpx.get(f"{BASE}/approvals", params={"scope": "requested"},
                  headers=hdr("agt_req"), timeout=10)
    ids = [a["id"] for a in r.json()]
    assert "apr_ghost" in ids and "apr_ok" in ids
    r = httpx.get(f"{BASE}/approvals", params={"scope": "inbox"},
                  headers=hdr("agt_req"), timeout=10)
    ids = [a["id"] for a in r.json()]
    assert "apr_ghost" not in ids and "apr_ok" not in ids
    r = httpx.get(f"{BASE}/approvals", params={"scope": "all"},
                  headers=hdr("agt_req"), timeout=10)
    ids = [a["id"] for a in r.json()]
    assert "apr_ghost" in ids and "apr_ok" in ids
    # requester can never decide its own send (even its own approval)
    r = httpx.post(f"{BASE}/approvals/apr_ok/decide",
                   json={"decision": "reject"}, headers=hdr("agt_req"),
                   timeout=10)
    assert r.status_code == 403, r.text


def test_stranger_scoped_out_and_cannot_decide(server):
    """Root rescue must not leak into a general grant: a non-root stranger
    sees nothing and can decide nothing."""
    for scope in ("inbox", "requested", "all"):
        r = httpx.get(f"{BASE}/approvals", params={"scope": scope},
                      headers=hdr("hum_clerk"), timeout=10)
        assert r.status_code == 200, (scope, r.text)
        assert r.json() == [], (scope, r.json())
    r = httpx.post(f"{BASE}/approvals/apr_ok/decide",
                   json={"decision": "reject"}, headers=hdr("hum_clerk"),
                   timeout=10)
    assert r.status_code == 403, r.text


def test_root_decides_orphan_approve(server):
    """Root rescue end-to-end: approve the orphaned approval and the real
    mail dispatches (sink adapter)."""
    r = httpx.post(f"{BASE}/approvals/apr_ghost/decide",
                   json={"decision": "approve"}, headers=hdr("hum_root"),
                   timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    msg = mc["treegent"].mail_messages.find_one({"_id": "mail_ghost"})
    mc.close()
    assert msg and msg["status"] == "sent", msg


def test_root_decides_orphan_reject(server):
    """Root rescue also un-sticks a dead-lettered send: reject closes the
    approval and fails the pending mail behind it."""
    r = httpx.post(f"{BASE}/approvals/apr_ghost2/decide",
                   json={"decision": "reject", "reason": "orphan cleanup"},
                   headers=hdr("hum_root"), timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    import pymongo
    mc = pymongo.MongoClient(os.environ["TG_TEST_MONGO_URL"])
    msg = mc["treegent"].mail_messages.find_one({"_id": "mail_ghost2"})
    mc.close()
    assert msg and msg["status"] == "failed", msg


def test_real_approver_still_decides(server):
    """Non-regression: the named approver's own decide path is unchanged."""
    r = httpx.post(f"{BASE}/approvals/apr_ok/decide",
                   json={"decision": "reject", "reason": "no"},
                   headers=hdr("agt_boss"), timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
