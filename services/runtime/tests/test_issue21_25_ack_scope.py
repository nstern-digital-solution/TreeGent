"""Issues #21/#25 — central-tier ack scope (runtime side).

The central ack path was the odd one out: /internal/agent-inbox never
emitted the inbox row id, so loop.py's collector recorded nothing and
_mark_consumed fell back to BLANKET acks — agent-inbox-delivered with an
empty inbox_ids list reads as "mark EVERY undelivered row", and an
unrecorded-wake fallback consumed wakes never collected. Any message
arriving mid-turn (or behind the pending page) was marked delivered
without ever being injected = silent loss. The crash safety net calls
_mark_consumed on FAILED turns too, which turned one silent-loss window
into one per crashed turn.

These tests drive the REAL central collect + ack path (no Mongo — the
service and db layers are call-recording doubles) and pin the contract:
the ack touches EXACTLY the ids collected this turn, nothing else.

Run: cd services/runtime && uv run --with pytest --with fastapi \
  --with httpx --with motor --with pydantic --with pyjwt --with pymongo \
  python -m pytest tests/ -q
"""
# pyright: reportAttributeAccessIssue=false
# (deliberate stub wiring below: fake service/db doubles, no Mongo)
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402

NOTIFY_LINE = ("You have 1 new message(s) waiting — use "
               "chat.check to read them.")


class StubCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class StubCollection:
    """Call-recording collection double (wake_events, transcripts, ...)."""

    def __init__(self, name, calls, rows=None):
        self.name, self.calls = name, calls
        self.rows = rows or []

    def _match(self, q):
        rows = self.rows
        reason = (q or {}).get("reason")
        if isinstance(reason, dict) and "$in" in reason:
            rows = [r for r in rows if r.get("reason") in reason["$in"]]
        return list(rows)

    def find(self, q, *args, **kwargs):
        self.calls.append((self.name, "find", q))
        return StubCursor(self._match(q))

    async def find_one(self, q, *args, **kwargs):
        self.calls.append((self.name, "find_one", q))
        return None

    async def update_one(self, q, update):
        self.calls.append((self.name, "update_one", q, update))

    async def insert_one(self, doc):
        self.calls.append((self.name, "insert_one", doc))

    async def insert_many(self, docs, **kwargs):
        self.calls.append((self.name, "insert_many", len(docs)))


class StubDB:
    """Mongo-free central db double; records every call."""

    def __init__(self, wakes=None):
        self.calls = []
        self.wake_events = StubCollection("wake_events", self.calls, wakes)
        self.mail_messages = StubCollection("mail_messages", self.calls)
        self.agents_runtime = StubCollection("agents_runtime", self.calls)
        self.agent_transcripts = StubCollection(
            "agent_transcripts", self.calls)
        # _cdb().client.treegent.agent_transcripts.insert_many(...) chain
        self.client = self
        self.treegent = self


class StubSvcs:
    """Chat + proxy double: canned agent-inbox, records every call."""

    def __init__(self, inbox_messages=None,
                 job_result=None):
        self.calls = []
        self.inbox = inbox_messages or []
        self.job_result = job_result or {
            "status": "done",
            "result": {"choices": [{"message": {"content": "ok."}}]}}

    async def _call(self, base, path, method="GET", body=None,
                    params=None, timeout=60.0):
        self.calls.append((method, path, body, params))
        if path == "/internal/agent-inbox":
            return {"messages": list(self.inbox)}
        if path == "/internal/agent-inbox-delivered":
            return {"ok": True}
        if path == "/v1/jobs":
            return {"job_id": "job1"}
        if path.startswith("/v1/jobs/"):
            return self.job_result
        raise AssertionError(f"unexpected service call: {path}")


def make_agent(svcs):
    """A CENTRAL-tier Agent wired for headless turns: real
    collect_injections + real _mark_consumed, stub IO."""
    a = loop.Agent.__new__(loop.Agent)
    a.name, a.id = "tester", "agt_test"
    a.messages = [{"role": "system", "content": "sys"}]
    a.busy = False
    a._reply_ctx = None
    a._pending_senders = []
    a.last_turn_end = loop.now()
    a._life_loaded = True            # _load_life becomes a no-op
    a.hc = None                      # central tier — the buggy ack path
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_central = False
    a._turn_lines = []
    a.workspace = tempfile.mkdtemp(prefix="ack25")
    a.svcs = svcs
    a.tool_calls = []

    async def _save_life():
        pass
    a._save_life = _save_life

    def _refresh_soul():
        pass
    a._refresh_soul = _refresh_soul
    return a


def wire(monkeypatch, db):
    """Route loop's lazy Mongo access at the doubles."""
    monkeypatch.setattr(loop, "_cdb", lambda: db)
    monkeypatch.setattr(loop, "turns",
                        StubCollection("runtime_turns", db.calls))


def delivered_posts(svcs):
    return [c[2] for c in svcs.calls
            if c[1] == "/internal/agent-inbox-delivered"]


def wake_updates(db):
    return [c[2] for c in db.calls
            if c[0] == "wake_events" and c[1] == "update_one"]


def blanket_wake_queries(db):
    """The old _mark_consumed re-queried ALL unconsumed wakes when no ids
    were recorded (collect's query always carries a reason filter)."""
    return [c[2] for c in db.calls
            if c[0] == "wake_events" and c[1] == "find"
            and "reason" not in c[2]]


def test_central_ack_is_scoped_to_collected_inbox_ids(monkeypatch):
    """#25: collect records the inbox row ids the server now emits
    (inbox_id) and the ack hits EXACTLY those — never the message id
    space (the delivered endpoint matches _id), never more."""
    svcs = StubSvcs(inbox_messages=[
        {"inbox_id": "in_1", "message_id": "msg_a",
         "sender_username": "ceo", "body": "one", "received_at": "t1"},
        {"inbox_id": "in_2", "message_id": "msg_b",
         "sender_username": "ceo", "body": "two", "received_at": "t2"},
    ])
    db = StubDB()
    a = make_agent(svcs)
    wire(monkeypatch, db)
    asyncio.run(a.run_turn("event"))

    posts = delivered_posts(svcs)
    assert len(posts) == 1, posts
    assert posts[0]["inbox_ids"] == ["in_1", "in_2"], posts[0]
    assert not set(posts[0]["inbox_ids"]) & {"msg_a", "msg_b"}, \
        "ack used message ids — the delivered endpoint matches the " \
        "INBOX ROW _id (issue #25)"
    assert wake_updates(db) == [], "inbox-only turn must not touch wakes"
    assert blanket_wake_queries(db) == [], \
        "ack must not re-query all unconsumed wakes (blanket fallback)"


def test_crashed_turn_acks_only_collected_ids(monkeypatch):
    """#25 crash safety net: a FAILED turn (generation dead) still acks
    what it collected (R68 — never retrigger the same wake forever) but
    must leave everything it did NOT collect pending. Before the fix the
    net blanket-consumed unrecorded wakes (wke_dm below) alongside."""
    svcs = StubSvcs(
        inbox_messages=[
            {"inbox_id": "in_9", "message_id": "msg_c",
             "sender_username": "ceo", "body": "one", "received_at": "t1"}],
        job_result={"status": "failed", "error": "proxy down"})
    db = StubDB(wakes=[{"_id": "wke_dm", "reason": "dm",
                        "message_id": "msg_d", "consumed": False}])
    a = make_agent(svcs)
    wire(monkeypatch, db)
    asyncio.run(a.run_turn("event"))

    posts = delivered_posts(svcs)
    assert len(posts) == 1, \
        "crashed turn must still ack its collected rows (R68 safety net)"
    assert posts[0]["inbox_ids"] == ["in_9"], posts[0]
    # issue #39/#40 changed this expectation: dm/mention wakes are NOW
    # fetched+recorded centrally, so the crashed-turn safety net acks
    # wke_dm (scoped to the recorded id). That is the spin fix — the wake
    # must not linger unconsumed (has_work -> 5s event loop forever).
    # The #25 guarantee the ORIGINAL assertion guarded (never ack a wake
    # the turn never collected) still holds, now expressed as: every acked
    # wake id is a COLLECTED id (id-scoped, not a blanket sweep).
    acked = {q[2]["_id"] for q in db.calls
             if q[1] == "update_one" and q[0] == "wake_events"}
    assert acked == {"wke_dm"}, \
        f"safety net acked an uncollected/wrong wake set: {acked}"
    assert blanket_wake_queries(db) == [], \
        "crash safety net must be id-scoped: no unconsumed-wakes sweep"


def test_wakes_only_turn_never_blanket_acks_inbox(monkeypatch):
    """#25: a turn that collected only wakes (no inbox rows) must NOT send
    an empty inbox_ids ack — the endpoint reads [] as 'mark EVERY
    undelivered row', silently acking messages that arrived mid-turn."""
    svcs = StubSvcs(inbox_messages=[])
    db = StubDB(wakes=[{"_id": "wke_1", "reason": "approval",
                        "approval_id": "apr_1", "detail": None,
                        "consumed": False}])
    a = make_agent(svcs)
    wire(monkeypatch, db)
    asyncio.run(a.run_turn("event"))

    assert delivered_posts(svcs) == [], \
        "empty inbox_ids ack = blanket ack of every undelivered row " \
        "(silent loss, issue #25) — skip the call instead"
    assert [q.get("_id") for q in wake_updates(db)] == ["wke_1"], \
        "the collected wake must be acked, exactly id-scoped"
