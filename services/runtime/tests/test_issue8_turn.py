"""Issue #8 rework (per review on #19): per-turn reply-context reset and
honest auto-post status lines — tested through the REAL turn loop and the
REAL _collect_injections_hosted structured path (issue #7's sender list).

Rosa's review verdict on #19: keep only the per-turn reset, drop the
defensive sentence parse (it re-introduced #7 once #18 landed), and make
the auto-post outcome honest — it was appended as a fabricated USER-role
message even on ERROR, so tool error text was read back as human turns.
"""
# pyright: reportAttributeAccessIssue=false
# (deliberate stub wiring below: fake host client / proxy, no Mongo)
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402

CENTRAL_LINE = ("You have 2 new message(s) waiting — use "
                "chat.check to read them.")


class StubHC:
    """Host-tier client: canned pending() inbox, records acks/reports."""
    def __init__(self, inbox):
        self._inbox = inbox
        self.calls = []

    async def pending(self, agent_id):
        return {"inbox": self._inbox, "wakes": []}

    async def delivered(self, agent_id, inbox_ids, wake_ids):
        self.calls.append(("delivered", list(inbox_ids)))

    async def turn_report(self, agent_id, report):
        self.calls.append(("turn_report", report.get("trigger")))

    async def push_transcript(self, agent_id, lines):
        self.calls.append(("push_transcript", len(lines)))


class StubSvcs:
    """Proxy job queue: one generation, plain answer, no tools."""
    def __init__(self):
        self.jobs = []

    async def _call(self, base, path, method="GET", body=None,
                    params=None, timeout=60.0):
        if path == "/v1/jobs":
            self.jobs.append(body)
            return {"job_id": "job1"}
        if path.startswith("/v1/jobs/"):
            return {"status": "done",
                    "result": {"choices": [{"message": {
                        "content": "ok."}}]}}
        raise AssertionError(f"unexpected service call: {path}")


def make_agent(inbox, stub_collect=None, tool_result="sent"):
    """An Agent wired for headless turns: real _run_turn_inner, stub IO.

    With stub_collect=None the REAL _collect_injections_hosted runs
    against StubHC's canned inbox (the structured sender path).
    """
    a = loop.Agent.__new__(loop.Agent)
    a.name, a.id = "tester", "agt_test"
    a.messages = [{"role": "system", "content": "sys"}]
    a.busy = False
    a._reply_ctx = None
    a._pending_senders = []
    a.last_turn_end = loop.now()
    a._life_loaded = True            # _load_life becomes a no-op
    a.hc = StubHC(inbox)
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_central = False
    a._turn_lines = []
    a.workspace = tempfile.mkdtemp(prefix="rc8")
    a.svcs = StubSvcs()
    a.gens = a.svcs.jobs             # generations reached this turn
    a.tool_calls = []                # (fn, args) seen by _exec_tool

    if stub_collect is not None:
        async def collect_injections():
            return list(stub_collect)
        a.collect_injections = collect_injections

    async def _exec_tool(fn, args):
        a.tool_calls.append((fn, args))
        return tool_result
    a._exec_tool = _exec_tool

    async def _save_life():
        pass
    a._save_life = _save_life

    def _refresh_soul():
        pass
    a._refresh_soul = _refresh_soul
    return a


def test_central_notification_no_crash():
    """Kept from #19/#8a: parsing the central notification sentence used to
    raise IndexError on every central event turn (caught as CRASHED, zero
    steps). The turn must still run. NOTE (issue #27): _reply_ctx is NO
    LONGER None on the central path — the central collect now derives it
    from structured sender data like hosted (see
    test_issue27_central_reply.py). This test keeps the crash guard with an
    EMPTY inbox (no rows => no sender => None, no crash)."""
    a = make_agent([], stub_collect=[CENTRAL_LINE])
    asyncio.run(a.run_turn("event"))
    assert a.gens, "turn must reach a generation (was: IndexError, 0 steps)"
    assert a._reply_ctx is None, "empty central notification => no sender"
    assert not [c for c in a.tool_calls if c[0] == "chat.send"], \
        "nothing to auto-post to with no sender"
    print("PASS central line: turn runs, no IndexError, empty inbox => None")


def test_reply_ctx_reset_per_turn():
    """Issue #8b (rewritten against the real structured path): _reply_ctx
    must not survive into a later turn — a heartbeat answer used to be
    auto-posted into the last DM conversation."""
    a = make_agent([{"inbox_id": "in1", "sender_username": "felix"}])
    asyncio.run(a.run_turn("event"))
    assert a._reply_ctx == {"sender": "felix"}, a._reply_ctx
    sends = [c for c in a.tool_calls if c[0] == "chat.send"]
    assert len(sends) == 1 and sends[0][1]["to"] == "felix", sends
    # a later heartbeat turn must start clean and not auto-post anywhere
    a.tool_calls.clear()
    asyncio.run(a.run_turn("heartbeat"))
    assert a._reply_ctx is None, "stale _reply_ctx survived into a new turn"
    assert a._pending_senders == [], "stale sender list survived the reset"
    assert not [c for c in a.tool_calls if c[0] == "chat.send"], \
        "heartbeat answer was auto-posted into the old conversation"
    print("PASS reset: _reply_ctx is per-turn, no cross-turn auto-post")


def test_reply_target_is_structured_sender():
    """Rewrite of #19's parse test against the structured path: the
    auto-post target is EXACTLY sender_username — never sentence garbage
    like 'alice — use chat.check to read them.' (#7's live repro)."""
    a = make_agent([{"inbox_id": "in1", "sender_username": "alice"}])
    asyncio.run(a.run_turn("event"))
    sends = [c for c in a.tool_calls if c[0] == "chat.send"]
    assert len(sends) == 1, sends
    assert sends[0][1]["to"] == "alice", sends
    assert "\u2014" not in sends[0][1]["to"], "sentence clause leaked (#7)"
    assert "chat.check" not in sends[0][1]["to"], "sentence clause leaked (#7)"
    print("PASS structured target: exactly 'alice', no sentence garbage")


def test_auto_post_status_lines_are_honest():
    """Rosa's third bug class: the outcome was appended as a fabricated
    user-role message even on ERROR — the model read
    'ERROR: no actor named …' as a human turn. It must be a status note
    (never user role), errors tagged [auto-post FAILED: …]."""
    ok = make_agent([{"inbox_id": "in1", "sender_username": "felix"}])
    asyncio.run(ok.run_turn("event"))
    notes = [m for m in ok.messages if "auto-post" in (m.get("content") or "")]
    assert notes, "success outcome must be recorded"
    assert notes[-1]["role"] != "user", notes[-1]
    assert "[auto-posted to felix" in notes[-1]["content"], notes[-1]

    bad = make_agent([{"inbox_id": "in1", "sender_username": "felix"}],
                     tool_result="ERROR: no actor named 'felix'")
    asyncio.run(bad.run_turn("event"))
    notes = [m for m in bad.messages if "auto-post" in (m.get("content") or "")]
    assert notes, "failure outcome must be recorded"
    assert notes[-1]["role"] != "user", notes[-1]
    assert notes[-1]["content"].startswith("[auto-post FAILED to felix"), \
        notes[-1]
    # and no fabricated user turn may carry the tool error text at all
    assert not [m for m in bad.messages
                if m.get("role") == "user"
                and "ERROR: no actor" in (m.get("content") or "")], \
        "tool error injected back as a fake user turn"
    print("PASS honest status lines: tagged, never fabricated user turns")
