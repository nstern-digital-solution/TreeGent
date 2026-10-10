"""Issue #13 regression: mail approval state machine crash windows.

decide() claims pending -> dispatching via find_one_and_update; any
kill/restart/deploy mid-request used to strand the approval in 'dispatching'
FOREVER (decide 409s on status != pending, no sweep existed), the requester
wake after the approved write was lost forever, and a manual reset after the
provider had already accepted the mail risked a SECOND send.

These tests drive the REAL decide() / dispatch_outbound() / recovery code
over an in-memory Mongo double: claim/dispatch/approve transitions, the
stale dispatching -> pending requeue (proxy R68 shape), outbound idempotency
(mail-id key), and the deterministic wke_appr_<approval_id> wake that
self-heals instead of being lost.
"""
# pyright: reportAttributeAccessIssue=false
# (deliberate stub wiring below: fake Mongo double, no real DB)
import asyncio
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from fastapi import HTTPException  # noqa: E402

from app import adapters, recovery  # noqa: E402
from app import main as mail_main  # noqa: E402
from app.main import DecideIn  # noqa: E402
from treegent_common.perms import RULE_SEEDS  # noqa: E402

REAL_OUTBOUND = dict(adapters.OUTBOUND)


# ---------------- in-memory Mongo double ----------------

class _Res:
    def __init__(self, modified=0, upserted=None):
        self.modified_count = modified
        self.upserted_id = upserted


def _cmp(a, b):
    """-1/0/1, or None when not comparable (Mongo type bracketing: a date
    $lt never matches a non-date)."""
    try:
        if a < b:
            return -1
        if a == b:
            return 0
        return 1
    except TypeError:
        return None


def _match_cond(v, cond):
    if isinstance(cond, dict):
        for op, want in cond.items():
            c = _cmp(v, want)
            if op == "$lt" and c != -1:
                return False
            if op == "$gt" and c != 1:
                return False
            if op == "$gte" and c not in (0, 1):
                return False
            if op == "$ne" and v == want:
                return False
            if op == "$in" and v not in want:
                return False
            if op not in ("$lt", "$gt", "$gte", "$ne", "$in"):
                raise AssertionError(f"fake mongo: unsupported op {op!r}")
        return True
    return v == cond


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                       reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeColl:
    def __init__(self):
        self.docs = {}

    def _match(self, doc, q):
        return all(_match_cond(doc.get(k), cond)
                   for k, cond in (q or {}).items())

    def _apply(self, doc, update, upserting):
        if upserting:
            doc.update(update.get("$setOnInsert") or {})
        doc.update(update.get("$set") or {})
        for k, v in (update.get("$inc") or {}).items():
            doc[k] = (doc.get(k) or 0) + v

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = dict(doc)
        return _Res()

    async def find_one(self, q):
        for d in self.docs.values():
            if self._match(d, q):
                return dict(d)
        return None

    async def find_one_and_update(self, q, update):
        for d in self.docs.values():
            if self._match(d, q):
                pre = dict(d)
                self._apply(d, update, upserting=False)
                return pre
        return None

    async def update_one(self, q, update, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                before = dict(d)
                self._apply(d, update, upserting=False)
                return _Res(modified=1 if d != before else 0)
        if upsert:
            doc = {k: v for k, v in (q or {}).items()
                   if not isinstance(v, dict)}
            self._apply(doc, update, upserting=True)
            self.docs[doc["_id"]] = doc
            return _Res(upserted=doc["_id"])
        return _Res()

    async def update_many(self, q, update):
        n = 0
        for d in self.docs.values():
            if self._match(d, q):
                before = dict(d)
                self._apply(d, update, upserting=False)
                if d != before:
                    n += 1
        return _Res(modified=n)

    def find(self, q=None):
        return FakeCursor([dict(d) for d in self.docs.values()
                           if self._match(d, q)])


class FakeDB:
    def __init__(self):
        self._colls = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeColl())


# ---------------- fixture ----------------

def make_env():
    """Wire the real modules' collection bindings to a fresh FakeDB."""
    db = FakeDB()
    for r in RULE_SEEDS:
        db.permissions.docs[r["_id"]] = dict(r)
    mail_main.db = db
    mail_main.approvals = db.approvals
    mail_main.mail_messages = db.mail_messages
    mail_main.mailboxes = db.mailboxes
    mail_main.actors = db.actors
    adapters.db = db
    adapters.mail_messages = db.mail_messages
    adapters.OUTBOUND = dict(REAL_OUTBOUND)
    return db


def seed(db, dispatching=False, decided_at=None):
    db.actors.docs["agt_req"] = {
        "_id": "agt_req", "kind": "agent",
        "org": {"parent_id": "agt_boss", "ancestors": ["agt_boss"]}}
    db.actors.docs["agt_boss"] = {
        "_id": "agt_boss", "kind": "agent",
        "org": {"parent_id": "root", "ancestors": ["root"]}}
    db.mailboxes.docs["mbx_1"] = {
        "_id": "mbx_1", "address": "req@treegent.local",
        "kind": "personal", "owner": "agt_req"}
    db.mail_messages.docs["mail_abc"] = {
        "_id": "mail_abc", "mailbox_id": "mbx_1", "direction": "out",
        "from_addr": "req@treegent.local", "to": "x@y.z", "subject": "hi",
        "text": "body", "status": "pending", "approval_id": "apr_xyz",
        "ts": recovery.now()}
    appr = {
        "_id": "apr_xyz", "action": "mail.send",
        "payload": {"mail_id": "mail_abc", "from": "req@treegent.local",
                    "to": "x@y.z", "subject": "hi"},
        "requester_id": "agt_req", "approver_id": "agt_boss",
        "status": "pending", "mode": "background",
        "created_at": recovery.now()}
    if dispatching:
        appr["status"] = "dispatching"
        appr["decided_at"] = decided_at or recovery.now()
    db.approvals.docs["apr_xyz"] = appr
    db.mail_adapters.docs["outbound-default"] = {
        "_id": "outbound-default", "direction": "outbound",
        "kind": "sink", "enabled": True}
    return db


def decide(decision="approve", reason=""):
    return asyncio.run(mail_main.decide(
        "apr_xyz", DecideIn(decision=decision, reason=reason),
        _c={"_id": "agt_boss"}))


def requeue(db):
    return asyncio.run(recovery.requeue_stale(db.approvals))


def heal(db):
    return asyncio.run(recovery.heal_lost_wakes(db.approvals, db.wake_events))


def wake(db):
    return db.wake_events.docs.get(recovery.appr_wake_id("apr_xyz"))


# ---------------- claim / dispatch / approve transitions ----------------

def test_approve_claims_dispatches_and_wakes_requester():
    db = make_env()
    seed(db)
    out = decide("approve")
    assert out["status"] == "approved", out
    assert db.approvals.docs["apr_xyz"]["status"] == "approved"
    m = db.mail_messages.docs["mail_abc"]
    assert m["status"] == "sent" and m["via"] == "sink", m
    # the send went out under the mail-id idempotency key (issue #13)
    assert m["idempotency_key"] == "mail_abc", m
    w = wake(db)
    assert w and w["agent_id"] == "agt_req", w
    assert w["reason"] == "approval" and w["consumed"] is False, w
    assert len(db.wake_events.docs) == 1
    print("PASS approve: claim -> dispatch -> approved + deterministic wake")


def test_send_failure_reopens_pending_and_retry_succeeds():
    db = make_env()
    seed(db)
    real = adapters.dispatch_outbound

    async def boom(msg):
        raise RuntimeError("provider down")

    adapters.dispatch_outbound = boom
    try:
        try:
            decide("approve")
            raise AssertionError("expected 502")
        except HTTPException as e:
            assert e.status_code == 502, e
    finally:
        adapters.dispatch_outbound = real
    assert db.approvals.docs["apr_xyz"]["status"] == "pending"
    assert db.mail_messages.docs["mail_abc"]["status"] == "send_failed"
    out = decide("approve")  # a retry re-runs the send, not a duplicate wake
    assert out["status"] == "approved", out
    assert len(db.wake_events.docs) == 1
    print("PASS send failure: back to pending, retry dispatches once")


def test_reject_wake_is_deterministic_and_self_heals():
    db = make_env()
    seed(db)
    out = decide("reject", reason="no budget")
    assert out["status"] == "rejected"
    assert db.approvals.docs["apr_xyz"]["status"] == "rejected"
    assert db.mail_messages.docs["mail_abc"]["status"] == "failed"
    w = wake(db)
    assert w and w["reason"] == "approval-rejected", w
    assert w["detail"] == "no budget", w
    # crash window 3 on the reject path: lost wake self-heals too
    del db.wake_events.docs[recovery.appr_wake_id("apr_xyz")]
    assert heal(db) == 1
    w = wake(db)
    assert w and w["reason"] == "approval-rejected" and w["detail"] == "no budget"
    print("PASS reject: deterministic wake, lost wake self-heals")


# ---------------- crash window 1: stale 'dispatching' ----------------

def test_stuck_dispatching_is_an_unrecoverable_409_without_recovery():
    """The reported defect: a claim that crashes before the send strands the
    approval in 'dispatching' — every decide 409s forever."""
    db = make_env()
    seed(db, dispatching=True)
    try:
        decide("approve")
        raise AssertionError("expected 409")
    except HTTPException as e:
        assert e.status_code == 409, e
    assert db.approvals.docs["apr_xyz"]["status"] == "dispatching"
    print("PASS defect repro: stranded dispatching approval 409s forever")


def test_stale_dispatching_requeued_then_redecidable():
    db = make_env()
    seed(db, dispatching=True,
         decided_at=recovery.now() - timedelta(seconds=301))
    assert requeue(db) == 1
    a = db.approvals.docs["apr_xyz"]
    assert a["status"] == "pending", a
    assert a["decided_at"] is None and a["requeues"] == 1, a
    out = decide("approve")
    assert out["status"] == "approved", out
    assert len(db.wake_events.docs) == 1
    print("PASS requeue: stale dispatching -> pending -> re-decidable")


def test_fresh_dispatching_is_not_requeued():
    db = make_env()
    seed(db, dispatching=True)  # just claimed — a live decide() is running
    assert requeue(db) == 0
    assert db.approvals.docs["apr_xyz"]["status"] == "dispatching"
    print("PASS requeue: a fresh claim is never stolen")


# ---------------- crash window 2: outbound idempotency ----------------

def test_post_send_crash_retry_never_calls_the_provider_again():
    """Crash after provider-accept + row update, before the approved write:
    the stale requeue re-exposes the approval; the re-decide must replay the
    recorded send instead of sending the real email a second time."""
    db = make_env()
    seed(db, dispatching=True,
         decided_at=recovery.now() - timedelta(seconds=301))
    db.mail_messages.docs["mail_abc"].update(
        {"status": "sent", "via": "resend", "provider_id": "prov_1",
         "idempotency_key": "mail_abc"})

    async def boom(msg):
        raise AssertionError("provider must NOT be called again")

    adapters.OUTBOUND = {"resend": boom, "sink": boom}
    try:
        assert requeue(db) == 1
        out = decide("approve")
    finally:
        adapters.OUTBOUND = dict(REAL_OUTBOUND)
    assert out["status"] == "approved" and out["mail"].get("replay"), out
    assert db.mail_messages.docs["mail_abc"]["provider_id"] == "prov_1"
    assert len(db.wake_events.docs) == 1
    print("PASS idempotent replay: post-send crash retry does not re-send")


def test_resend_idempotency_key_is_the_mail_id_on_every_attempt():
    db = make_env()
    seed(db)
    calls = []

    def fake_sync(payload, idempotency_key=""):
        calls.append((payload, idempotency_key))
        return {"status": "sent", "via": "resend", "provider_id": "prov_9"}

    real = adapters._send_resend_sync
    adapters._send_resend_sync = fake_sync
    try:
        msg = dict(db.mail_messages.docs["mail_abc"])
        r1 = asyncio.run(adapters.send_resend(msg))
        r2 = asyncio.run(adapters.send_resend(msg))  # crash-retry
    finally:
        adapters._send_resend_sync = real
    assert [c[1] for c in calls] == ["mail_abc", "mail_abc"], calls
    assert calls[0][0] == calls[1][0], "payload must be identical on retry"
    assert r1["provider_id"] == r2["provider_id"] == "prov_9"
    print("PASS resend: Idempotency-Key = mail id, stable across retries")


# ---------------- crash window 3: lost requester wake ----------------

def test_lost_requester_wake_self_heals_exactly_once():
    db = make_env()
    seed(db)
    a = db.approvals.docs["apr_xyz"]
    a["status"] = "approved"
    a["decided_at"] = recovery.now() - timedelta(seconds=60)
    # crash after the approved write, before the wake insert
    assert heal(db) == 1
    w = wake(db)
    assert w and w["agent_id"] == "agt_req" and w["consumed"] is False, w
    assert "x@y.z" in w["detail"], w
    # the 30s sweep must never duplicate it
    assert heal(db) == 0
    assert len(db.wake_events.docs) == 1
    print("PASS lost wake: heals once via wke_appr_<approval_id>, never dup")


def test_wake_heal_skips_old_and_already_woken_approvals():
    db = make_env()
    seed(db)
    a = db.approvals.docs["apr_xyz"]
    a["status"] = "approved"
    a["decided_at"] = recovery.now() - timedelta(seconds=7200)  # stale news
    assert heal(db) == 0
    # in-window, but a pre-fix random-id wake already references it
    a["decided_at"] = recovery.now() - timedelta(seconds=60)
    db.wake_events.docs["wke_legacy01"] = {
        "_id": "wke_legacy01", "agent_id": "agt_req",
        "approval_id": "apr_xyz", "reason": "approval", "consumed": False}
    assert heal(db) == 0
    assert len(db.wake_events.docs) == 1
    print("PASS wake heal: bounded window, legacy wakes count as delivered")


def test_wake_id_is_deterministic():
    assert recovery.appr_wake_id("apr_xyz") == "wke_appr_apr_xyz"
    assert (recovery.appr_wake_id("apr_xyz")
            == recovery.appr_wake_id("apr_xyz"))
    print("PASS wake id: deterministic wke_appr_<approval_id>")
