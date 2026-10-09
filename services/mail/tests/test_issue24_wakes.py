"""Issue #24: production pull-sync (sync_inbound) never woke shared-mailbox
members, and wake ids were per-MAIL — a second member collided on the same
id and was never woken; the id also truncated the 36-char provider UUID
to 24 chars.

These tests run the REAL sync_inbound + ingest_inbound against an
in-memory stub DB and a stub provider listing (no Mongo, no network).
"""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import adapters  # noqa: E402

# 36-char provider UUID — the old wke_pull_{id[:24]} truncated it
MAIL_ID = "11111111-2222-3333-4444-555555555555"
ROW = {"id": MAIL_ID, "to": ["team@example.com"], "from": "sender@example.com",
       "subject": "numbers", "created_at": "2026-10-01T08:00:00.000Z"}


class MiniCol:
    """Just enough Mongo collection semantics for sync_inbound: equality
    find_one, insert_one, and update_one with $set/$setOnInsert + upsert."""

    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    async def find_one(self, q):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            doc = dict(q)
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            self.docs.append(doc)


class FakeDB:
    def __init__(self, mailboxes=(), actors=()):
        self.mailboxes = MiniCol(mailboxes)
        self.actors = MiniCol(actors)
        self.mail_messages = MiniCol()
        self.wake_events = MiniCol()


def wire(monkeypatch, mailboxes, actors, rows=None):
    """Install stub DB + stub provider listing into the adapters module."""
    db = FakeDB(mailboxes, actors)
    monkeypatch.setattr(adapters, "db", db)
    monkeypatch.setattr(adapters, "mail_messages", db.mail_messages)
    monkeypatch.setattr(adapters, "list_received_emails",
                        lambda limit=100, after="": {
                            "data": list(ROW for _ in range(1))
                            if rows is None else list(rows),
                            "has_more": False})
    monkeypatch.setattr(adapters, "fetch_received_email",
                        lambda email_id: _ok({"text": "hello"}))
    return db


async def _ok(value):
    return value


def wake_ids(db):
    return {w["_id"] for w in db.wake_events.docs}


def woke_agents(db):
    return {w["agent_id"] for w in db.wake_events.docs}


def test_shared_mailbox_wakes_every_agent_member(monkeypatch):
    """The defect: `if mb.get("kind") == "personal"` woke only personal
    owners — shared members were never woken in production."""
    db = wire(monkeypatch,
              mailboxes=[{"_id": "mbx_1", "address": "team@example.com",
                          "kind": "shared", "owner": "agt_owner",
                          "members": ["agt_a", "agt_b", "hum_c"]}],
              actors=[{"_id": "agt_owner", "kind": "agent"},
                      {"_id": "agt_a", "kind": "agent"},
                      {"_id": "agt_b", "kind": "agent"},
                      {"_id": "hum_c", "kind": "human"}])
    out = asyncio.run(adapters.sync_inbound())
    assert out["imported"] == 1, out
    # every AGENT member woken (dev-hook shape), non-members and humans not
    assert woke_agents(db) == {"agt_a", "agt_b"}, db.wake_events.docs


def test_personal_mailbox_wakes_agent_owner_only(monkeypatch):
    db = wire(monkeypatch,
              mailboxes=[{"_id": "mbx_2", "address": "team@example.com",
                          "kind": "personal", "owner": "agt_o",
                          "members": []}],
              actors=[{"_id": "agt_o", "kind": "agent"}])
    asyncio.run(adapters.sync_inbound())
    assert woke_agents(db) == {"agt_o"}, db.wake_events.docs


def test_non_agent_recipients_are_never_woken(monkeypatch):
    """Agent-kind filter applies to every mailbox kind (matches the dev
    hook): a human owner/member must not get an agent wake event."""
    db = wire(monkeypatch,
              mailboxes=[{"_id": "mbx_3", "address": "team@example.com",
                          "kind": "shared", "owner": "hum_owner",
                          "members": ["hum_a"]},
                         {"_id": "mbx_4", "address": "solo@example.com",
                          "kind": "personal", "owner": "hum_b",
                          "members": []}],
              actors=[{"_id": "hum_owner", "kind": "human"},
                      {"_id": "hum_a", "kind": "human"},
                      {"_id": "hum_b", "kind": "human"}],
              rows=[ROW, dict(ROW, id="22222222-2222-3333-4444-555555555555",
                              to=["solo@example.com"])])
    asyncio.run(adapters.sync_inbound())
    assert db.wake_events.docs == [], db.wake_events.docs


def test_wake_ids_unique_per_recipient_and_untruncated(monkeypatch):
    """Second defect: wke_pull_{mail_id[:24]} was per-MAIL — two members
    collided on one id (second never woken) — and truncated the 36-char
    provider UUID. Ids must be wke_pull_<mailid>_<recipient>."""
    db = wire(monkeypatch,
              mailboxes=[{"_id": "mbx_5", "address": "team@example.com",
                          "kind": "shared", "owner": "agt_owner",
                          "members": ["agt_a", "agt_b"]}],
              actors=[{"_id": "agt_a", "kind": "agent"},
                      {"_id": "agt_b", "kind": "agent"}])
    asyncio.run(adapters.sync_inbound())
    ids = wake_ids(db)
    assert ids == {f"wke_pull_{MAIL_ID}_agt_a",
                   f"wke_pull_{MAIL_ID}_agt_b"}, db.wake_events.docs
    assert len(ids) == 2, "per-recipient ids must not collide"
    for wid in ids:
        assert MAIL_ID in wid, f"provider id truncated in {wid}"
    assert woke_agents(db) == {"agt_a", "agt_b"}


def test_resync_is_idempotent(monkeypatch):
    """Re-syncing the same provider mail must not duplicate mail rows or
    wakes (dedupe + $setOnInsert)."""
    db = wire(monkeypatch,
              mailboxes=[{"_id": "mbx_6", "address": "team@example.com",
                          "kind": "shared", "owner": "agt_owner",
                          "members": ["agt_a", "agt_b"]}],
              actors=[{"_id": "agt_a", "kind": "agent"},
                      {"_id": "agt_b", "kind": "agent"}])
    asyncio.run(adapters.sync_inbound())
    asyncio.run(adapters.sync_inbound())
    assert len(db.mail_messages.docs) == 1, db.mail_messages.docs
    assert len(db.wake_events.docs) == 2, db.wake_events.docs
