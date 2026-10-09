"""Issue #26 pull-sync hygiene regressions: persisted watermark (no
full-history rescan on every read), body-heal attempts cap +
healed-then-wake, multi-recipient routing with per-mailbox dedupe, and the
in-flight sync guard. Fake provider + in-memory fake DB — no Mongo, no
network (same stub style as services/runtime/tests)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import adapters  # noqa: E402

BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(age_s: int) -> str:
    """age_s seconds before BASE -> provider-style ISO ts (newest = age 0)."""
    return (BASE - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")


# ---------------- fakes ----------------

def _match(doc: dict, q: dict) -> bool:
    """Tiny Mongo-query matcher for the operators the sync path uses."""
    for k, v in q.items():
        if k == "$or":
            if not any(_match(doc, c) for c in v):
                return False
            continue
        if k == "$and":
            if not all(_match(doc, c) for c in v):
                return False
            continue
        have, val = k in doc, doc.get(k)
        if isinstance(v, dict) and any(str(o).startswith("$") for o in v):
            for op, arg in v.items():
                if op == "$exists":
                    if bool(have) != bool(arg):
                        return False
                elif op == "$ne":
                    if have and val == arg:
                        return False
                elif op == "$lt":
                    if not have or not val < arg:
                        return False
                elif op == "$in":
                    if (val if have else None) not in arg:
                        return False
                else:
                    raise AssertionError(f"fake matcher lacks {op}")
        elif not have or val != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a):
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __aiter__(self):
        self._it = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeColl:
    def __init__(self):
        self.docs = []

    def _find(self, q):
        return [d for d in self.docs if _match(d, q)]

    async def find_one(self, q):
        m = self._find(q)
        return dict(m[0]) if m else None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, u, upsert=False):
        m = self._find(q)
        if m:
            d = m[0]
            for k, v in (u.get("$set") or {}).items():
                d[k] = v
            for k, v in (u.get("$inc") or {}).items():
                d[k] = d.get(k, 0) + v
            return  # $setOnInsert applies only on insert
        if upsert:
            doc = {k: v for k, v in q.items() if not str(k).startswith("$")}
            doc.update(u.get("$setOnInsert") or {})
            doc.update(u.get("$set") or {})
            self.docs.append(doc)

    def find(self, q=None):
        return FakeCursor(self._find(q or {}))


class FakeDB:
    def __init__(self):
        self.mailboxes = FakeColl()
        self.mail_messages = FakeColl()
        self.mail_sync_state = FakeColl()
        self.wake_events = FakeColl()
        self.actors = FakeColl()


class FakeProvider:
    """Resend receiving API double: newest-first listing, cursor paging."""

    def __init__(self, mails):
        self.mails = mails            # [{id, to, from, created_at}, ...]
        self.list_calls = 0
        self.fetch_calls = []
        self.bodies = {}              # id -> str | Exception

    def list_received_emails(self, limit=100, after=""):
        self.list_calls += 1
        idx = next((i for i, m in enumerate(self.mails)
                    if m["id"] == after), None)
        start = (idx + 1) if idx is not None else 0
        page = self.mails[start:start + limit]
        return {"object": "list", "data": page,
                "has_more": start + limit < len(self.mails)}

    async def fetch_received_email(self, email_id):
        self.fetch_calls.append(email_id)
        body = self.bodies.get(email_id, "")
        if isinstance(body, Exception):
            raise body
        return {"text": body}


def mail(i, to, text_id=None, frm="sender@example.com"):
    return {"id": text_id or f"{i:08d}-1111-2222-3333-444445555666",
            "to": [to], "from": frm, "subject": f"s{i}",
            "created_at": iso(i)}


def setup(monkeypatch, provider, mailboxes=(), actors=()):
    db = FakeDB()
    for mb in mailboxes:
        db.mailboxes.docs.append(dict(mb))
    for a in actors:
        db.actors.docs.append(dict(a))
    monkeypatch.setattr(adapters, "db", db)
    monkeypatch.setattr(adapters, "mail_messages", db.mail_messages)
    monkeypatch.setattr(adapters, "list_received_emails",
                        provider.list_received_emails)
    monkeypatch.setattr(adapters, "fetch_received_email",
                        provider.fetch_received_email)
    monkeypatch.setattr(adapters, "_inbound_busy", False)
    return db


MB_A = {"_id": "mbx_a", "address": "a@example.com", "kind": "personal",
        "owner": "agt_a"}
MB_B = {"_id": "mbx_b", "address": "b@example.com", "kind": "personal",
        "owner": "agt_b"}
AGT_A = {"_id": "agt_a", "kind": "agent"}
AGT_B = {"_id": "agt_b", "kind": "agent"}


# ---------------- tests ----------------

def test_watermark_advance_no_full_rescan(monkeypatch):
    """#26 core: the first sync pages the whole history ONCE; every later
    sync stops at the watermark (one listing page) instead of re-listing
    everything — and genuinely new mail is still imported."""
    mails = [mail(i, "a@example.com") for i in range(250)]  # 3 pages
    p = FakeProvider(mails)
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    r1 = asyncio.run(adapters.sync_inbound())
    assert r1["imported"] == 250
    assert p.list_calls == 3            # full first walk (3 pages)
    assert db.mail_sync_state.docs, "watermark must be persisted"
    wm = adapters._parse_watermark(db.mail_sync_state.docs[0]["last_created_at"])
    assert wm == BASE

    calls = p.list_calls
    r2 = asyncio.run(adapters.sync_inbound())
    assert r2["imported"] == 0
    assert p.list_calls == calls + 1, \
        "second sync must stop at the watermark (1 page), not rescan all"

    p.mails.insert(0, mail(-1, "a@example.com",
                           text_id="99999999-1111-2222-3333-444445555666"))
    calls = p.list_calls
    r3 = asyncio.run(adapters.sync_inbound())
    assert r3["imported"] == 1
    assert p.list_calls == calls + 1, "new mail found in a single page"
    assert len(db.mail_messages.docs) == 251


def test_scoped_sync_does_not_advance_watermark(monkeypatch):
    """A mailbox-scoped sync must not mark OTHER mailboxes' mail as synced
    (the /mailboxes/{id}/messages read passes an addresses filter)."""
    mails = [mail(0, "b@example.com")]
    p = FakeProvider(mails)
    db = setup(monkeypatch, p, [MB_A, MB_B], [AGT_A, AGT_B])

    r1 = asyncio.run(adapters.sync_inbound(["a@example.com"]))
    assert r1["imported"] == 0
    assert not db.mail_sync_state.docs, "scoped sync must not save a watermark"

    r2 = asyncio.run(adapters.sync_inbound())
    assert r2["imported"] == 1, "full sync still sees the mail for b@"

    rows = db.mail_messages.docs
    assert len(rows) == 1 and rows[0]["mailbox_id"] == "mbx_b"


def test_body_heal_attempts_capped(monkeypatch):
    """#26 defect 2: bodyless rows were re-fetched forever. Now each row
    gets at most BODY_ATTEMPTS_CAP heal attempts, then stays bodyless and
    is never fetched again."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = Exception("resend body hiccup")
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    seeded = {"_id": "mail_seed", "mailbox_id": "mbx_a", "direction": "in",
              "from_addr": m["from"], "to": "a@example.com", "subject": "s",
              "text": "", "resend_id": m["id"], "ts": BASE}
    db.mail_messages.docs.append(dict(seeded))

    for _ in range(6):
        asyncio.run(adapters.sync_inbound())

    heals = [c for c in p.fetch_calls if c == m["id"]]
    assert len(heals) == adapters.BODY_ATTEMPTS_CAP, \
        f"expected {adapters.BODY_ATTEMPTS_CAP} heal attempts, got {len(heals)}"
    row = db.mail_messages.docs[0]
    assert (row.get("text") or "") == ""
    assert row.get("body_attempts") == adapters.BODY_ATTEMPTS_CAP
    assert not db.wake_events.docs, "an unhealed body wakes nobody"


def test_healed_body_wakes_once(monkeypatch):
    """#26 defect 2: `continue` before the wake block meant a healed body
    never notified. Now a body that arrives later wakes every agent
    recipient exactly once — deterministic wke_pull_<mailid>_<recipient>
    id + $setOnInsert (convention shared with #24)."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = Exception("later")
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    db.mail_messages.docs.append(
        {"_id": "mail_seed", "mailbox_id": "mbx_a", "direction": "in",
         "from_addr": m["from"], "to": "a@example.com", "subject": "s",
         "text": "", "resend_id": m["id"], "ts": BASE})

    asyncio.run(adapters.sync_inbound())          # heal attempt fails
    assert not db.wake_events.docs

    p.bodies[m["id"]] = "the healed body"          # provider recovers
    asyncio.run(adapters.sync_inbound())
    assert db.mail_messages.docs[0]["text"] == "the healed body"

    wakes = db.wake_events.docs
    assert len(wakes) == 1
    assert wakes[0]["_id"] == adapters.pull_wake_id(m["id"], "agt_a")
    assert wakes[0]["agent_id"] == "agt_a" and wakes[0]["reason"] == "mail"

    asyncio.run(adapters.sync_inbound())          # healed rows never re-wake
    assert len(db.wake_events.docs) == 1
    assert len([c for c in p.fetch_calls if c == m["id"]]) == 2


def test_multi_recipient_routing_per_mailbox_dedupe(monkeypatch):
    """#26 defect 3: to_list[0]-only routing dropped every recipient after
    the first and the global resend_id dedupe swallowed them forever. One
    provider mail now imports once per matching mailbox, and the dedupe is
    per mailbox."""
    m = {"id": "00000000-1111-2222-3333-444445555666",
         "to": ["a@example.com", "b@example.com"],
         "from": "sender@example.com", "subject": "s", "created_at": iso(0)}
    p = FakeProvider([m])
    p.bodies[m["id"]] = "hi both"
    db = setup(monkeypatch, p, [MB_A, MB_B], [AGT_A, AGT_B])

    r1 = asyncio.run(adapters.sync_inbound())
    assert r1["imported"] == 2
    rows = sorted(db.mail_messages.docs, key=lambda d: d["mailbox_id"])
    assert [d["mailbox_id"] for d in rows] == ["mbx_a", "mbx_b"]
    assert all(d["text"] == "hi both" for d in rows)
    assert len([c for c in p.fetch_calls if c == m["id"]]) == 1, \
        "body fetched once per provider mail, not per recipient"

    r2 = asyncio.run(adapters.sync_inbound())
    assert r2["imported"] == 0
    assert len(db.mail_messages.docs) == 2, "per-mailbox dedupe on re-sync"


def test_inflight_sync_serves_cache(monkeypatch):
    """#26 defect 4: concurrent /mailboxes calls double-synced the whole
    history. The second caller now serves the cache while a walk runs."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = "body"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    started, gate = asyncio.Event(), asyncio.Event()

    async def slow_fetch(email_id):
        started.set()
        await gate.wait()
        return {"text": "body"}

    monkeypatch.setattr(adapters, "fetch_received_email", slow_fetch)

    async def main():
        t1 = asyncio.create_task(adapters.sync_inbound())
        await started.wait()                     # t1 holds the in-flight flag
        r2 = await adapters.sync_inbound()       # must NOT double-sync
        gate.set()
        r1 = await t1
        return r1, r2

    r1, r2 = asyncio.run(main())
    assert r2["imported"] == 0 and r2["backfilled"] == 0
    assert r1["imported"] == 1
    assert p.list_calls == 1, "the racing caller never listed the provider"
    assert len(db.mail_messages.docs) == 1, "no duplicate import"
