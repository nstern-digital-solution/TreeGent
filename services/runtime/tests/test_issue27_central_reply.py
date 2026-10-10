"""Issue #27: central-tier reply context — DM auto-reply must work on the
central path exactly as it does hosted.

Root defect (filed #27, proven by execution in the issue): the reply
target (_reply_ctx) was assigned ONLY in _collect_injections_hosted
(loop.py:290-292). The central collect rendered a sender-less notification
and set no reply context, so turn-end auto-post (`if self._reply_ctx`)
never fired for a central agent — a human DMing a central agent got
silence, with no error anywhere. The payload already carried
sender_username (chat internal.py:40); central simply never read it.

These tests run the REAL Agent.collect_injections central branch (hc=None);
only the HTTP edge (svcs._call) and Mongo (_cdb) are stubbed.
"""
# pyright: reportAttributeAccessIssue=false
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **k):
        return self

    def limit(self, *a):
        return self

    def __aiter__(self):
        async def g():
            for r in self._rows:
                yield r
        return g()


class StubSvcs:
    def __init__(self, messages):
        self._messages = messages
        self.acks = []

    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        if path == "/internal/agent-inbox":
            return {"messages": self._messages}
        if path == "/internal/agent-inbox-delivered":
            self.acks.append(body)
            return {"marked": len(body.get("inbox_ids", []))}
        raise AssertionError(f"unexpected service call: {path}")


class StubWakeCol:
    def __init__(self, rows):
        self._rows = rows

    def find(self, q, *a, **k):
        f = (q.get("reason") or {}).get("$in")
        rows = [r for r in self._rows if f is None or r.get("reason") in f]
        return _Cur(rows)

    async def update_one(self, q, upd, *a, **k):
        return None


class StubDB:
    def __init__(self, wakes=()):
        self.wake_events = StubWakeCol(list(wakes))
        self.mail_messages = self

    async def find_one(self, *a, **k):
        return None


def central_agent(messages, wakes=()):
    a = loop.Agent.__new__(loop.Agent)
    a.id, a.name, a.hc = "agt_central", "tester", None   # hc=None => central
    a.svcs = StubSvcs(messages)
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_central = False
    a._reply_ctx, a._pending_senders = None, []
    loop._cdb = lambda: StubDB(wakes)
    return a


def test_central_single_sender_sets_reply_ctx():
    a = central_agent([{"inbox_id": "in1", "sender_username": "felix",
                        "message_id": "m1", "body": "ping"}])
    lines = asyncio.run(a.collect_injections())
    assert a._reply_ctx == {"sender": "felix"}, a._reply_ctx
    assert a._pending_senders == ["felix"], a._pending_senders
    assert any("from felix" in ln for ln in lines), lines
    print("PASS #27: central single-sender sets _reply_ctx + names sender")


def test_central_two_senders_no_reply_ctx():
    a = central_agent([
        {"inbox_id": "in1", "sender_username": "anna", "message_id": "m1"},
        {"inbox_id": "in2", "sender_username": "felix", "message_id": "m2"}])
    lines = asyncio.run(a.collect_injections())
    assert a._reply_ctx is None, a._reply_ctx
    assert a._pending_senders == ["anna", "felix"], a._pending_senders
    assert any("from anna, felix" in ln for ln in lines), lines
    print("PASS #27: central two senders -> no reply ctx, both named")


def test_central_repeated_sender_is_single():
    a = central_agent([
        {"inbox_id": "in1", "sender_username": "felix", "message_id": "m1"},
        {"inbox_id": "in2", "sender_username": "felix", "message_id": "m2"}])
    asyncio.run(a.collect_injections())
    assert a._reply_ctx == {"sender": "felix"}, a._reply_ctx
    print("PASS #27: repeated sender collapses to one reply target")


def test_central_no_inbox_keeps_reply_ctx_none():
    a = central_agent([])
    lines = asyncio.run(a.collect_injections())
    assert a._reply_ctx is None
    assert a._pending_senders == []
    assert lines == [], lines
    print("PASS #27: empty inbox -> no reply ctx, no crash")


def test_central_row_ids_recorded_for_id_scoped_ack():
    a = central_agent([
        {"inbox_id": "in1", "sender_username": "felix", "message_id": "m1"},
        {"inbox_id": "in2", "sender_username": "felix", "message_id": "m2"}])
    asyncio.run(a.collect_injections())
    assert a._pending_inbox_ids == ["in1", "in2"], a._pending_inbox_ids
    print("PASS #27: central collect records inbox row ids (ack scope)")
