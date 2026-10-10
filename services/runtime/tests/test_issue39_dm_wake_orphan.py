"""Issue #39: central collect must fetch+record dm/mention wakes.

chat _dispatch writes reason='dm'/'mention' wake rows for central agents,
but the central wake query filtered them out ($in missed them). With PR
#31's strict id-scoped acks — which correctly end the blanket fallback —
nothing else ever consumed those rows: one DM left has_work True forever
(no reason filter there) and the supervisor fired an empty event turn
every WAKE_POLL_S=5s for the wake's 7-day TTL, heartbeats starved.

Invariant (the complement of PR #31's blanket-ack test): every wake
has_work() counts is consumed by some collect path. Hosted already
complied (ack dm/mention ids with no rendered line); central now mirrors
it.
"""
# pyright: reportAttributeAccessIssue=false
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402

DM_WAKE = {"_id": "wke_dm_1", "agent_id": "agt_central", "reason": "dm",
           "conversation_id": "dm_a__b", "created_at": None,
           "consumed": False}
MENTION_WAKE = {"_id": "wke_mn_1", "agent_id": "agt_central",
                "reason": "mention", "conversation_id": "cnv_1",
                "created_at": None, "consumed": False}


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


class StubWakeCol:
    def __init__(self):
        self.rows = [dict(DM_WAKE), dict(MENTION_WAKE)]
        self.consumed = []

    def find(self, q, *a, **k):
        f = (q.get("reason") or {}).get("$in")
        rows = [r for r in self.rows
                if r.get("consumed")
                and False or f is None or r.get("reason") in f]
        return _Cur(rows)

    async def update_one(self, q, upd, *a, **k):
        # record what the ack consumed
        self.consumed.append(dict(q))
        return None


class StubSvcs:
    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        if path == "/internal/agent-inbox":
            return {"messages": []}          # content already injected
        if path == "/internal/agent-inbox-delivered":
            return {"marked": 0}
        raise AssertionError(path)


class StubDB:
    def __init__(self):
        self.wake_events = StubWakeCol()
        self.mail_messages = self

    async def find_one(self, *a, **k):
        return None


def central_agent(db):
    a = loop.Agent.__new__(loop.Agent)
    a.id, a.name, a.hc = "agt_central", "tester", None
    a.svcs = StubSvcs()
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_central = False
    a._reply_ctx, a._pending_senders = None, []
    loop._cdb = lambda: db
    return a


def test_dm_and_mention_wakes_are_recorded():
    db = StubDB()
    a = central_agent(db)
    lines = asyncio.run(a.collect_injections())
    ids = sorted(a._pending_wake_ids)
    assert ids == ["wke_dm_1", "wke_mn_1"], (
        f"dm/mention wakes not recorded: {ids}")
    # NO rendered lines for them (content rides in inbox rows)
    assert lines == [], lines
    print("PASS #39: dm/mention recorded, no double-render")


def test_ack_consumes_exactly_those_ids():
    """The recorded ids flow through _mark_consumed's central branch —
    closing the spin loop."""
    db = StubDB()
    a = central_agent(db)
    asyncio.run(a.collect_injections())
    asyncio.run(a._mark_consumed())
    q = db.wake_events.consumed
    assert q, "central ack must touch wake_events"
    # shape-agnostic: base consumes per-id ({_id: wid}), PR #31 bulk
    # form is {_id: {$in: [...]}} — the invariant is WHICH ids die.
    got = set()
    for query in q:
        i = query.get("_id")
        if isinstance(i, dict):
            got |= set(i.get("$in", []))
        elif isinstance(i, str):
            got.add(i)
    assert got == {"wke_dm_1", "wke_mn_1"}, q
    print("PASS #39: ack consumes the dm/mention ids (scoped)")


def test_query_covers_every_reason_has_work_counts():
    """Invariant guard: the central $in list must contain every reason
    has_work's unfiltered wake check can see. If a new wake reason is
    ever added, this pins the list so review notices — the orphan class
    cannot silently regrow."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "app", "loop.py")).read()
    m = re.search(r'wake_events\.find\(\s*\{"agent_id": self\.id, "consumed": False,\s*"reason": \{"\$in": \[([^\]]+)\]', src)
    assert m, "central wake query shape changed — update this guard"
    reasons = set(re.findall(r'"([a-z-]+)"', m.group(1)))
    assert {"mail", "approval", "approval-rejected", "dm", "mention"} <= reasons, reasons
    print("PASS #39: query covers all known wake reasons:", sorted(reasons))
