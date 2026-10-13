"""R78: token-based compaction trigger — targeting, boundary table, and
the measurement capture/persist loop.

The provider-reported usage.prompt_tokens of the last MAIN-LOOP
generation is the trigger's only real signal: compact at > 50% of the
model's context window (strictly greater), land at <= 25% after, with a
2000-message backstop. Chars are a retired proxy and must not matter.
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import compact as C  # noqa: E402
from app import hostclient, loop, qtree  # noqa: E402
from app import soul as SOUL  # noqa: E402


# ---- needs_compaction boundary table ---------------------------------------

def test_needs_compaction_boundary_table():
    def msgs(n):
        return [{"role": "system", "content": "sys"}] + \
               [{"role": "user", "content": f"m{i}"} for i in range(n)]
    table = [
        # (label, non_sys count, prompt_tokens, context_window, expected)
        ("0.49x window",        5,  49_000, 100_000, False),
        ("0.50x exactly",       5,  50_000, 100_000, False),  # strictly >
        ("0.51x window",        5,  51_000, 100_000, True),
        ("0.50x big window",    5, 100_000, 200_000, False),
        ("0.50x+1 big window",  5, 100_001, 200_000, True),
        ("missing usage",    1999,    None,    None, False),  # backstop only
        ("zero usage",       1999,       0,       0, False),
        ("backstop 1999",    1999,  90_000, 100_000, True),   # token fires
        ("backstop 1999 no tok", 1999, None, None,  False),
        ("backstop 2000",    2000,    None,    None, True),
        ("backstop 2001",    2001,    None,    None, True),
    ]
    for label, n, pt, cw, expected in table:
        got = qtree.needs_compaction(msgs(n), pt, cw)
        assert got is expected, f"{label}: got {got}, expected {expected}"
    # char-irrelevance: 10M chars + tiny tokens + few msgs = NO compaction
    huge = [{"role": "system", "content": "s"},
            {"role": "user", "content": "x" * 5_000_000},
            {"role": "assistant", "content": "y" * 5_000_000}]
    assert not qtree.needs_compaction(huge, 100, 100_000), \
        "10M chars + tiny tokens must NOT compact (char trigger is retired)"
    print("PASS needs_compaction boundary table (11 rows + char irrelevance)")


# ---- maybe_compact token targeting ----------------------------------------

class StubAgent:
    def __init__(self, msgs, pt=0, cw=0):
        self.messages = msgs
        self.last_prompt_tokens = pt
        self.context_window = cw
        self.workspace = tempfile.mkdtemp(prefix="r78_tok")
        self.regened = False

    def _regen_system_prompt(self):
        self.regened = True


async def _ok_gen(*_a):
    return "stub summary"


def test_token_targeting_lands_at_25pct_and_keeps_newest():
    """Token path drops the OLDEST until the char-proportion estimate
    (est = pt * kept_chars/total_chars) lands at <= 25% of the window —
    and no further: keeping one dropped message more would overshoot.
    The kept set is the NEWEST tail."""
    C._gen = _ok_gen
    non_sys, sizes = [], []
    for i in range(40):
        n = 10 + (i * 37) % 191          # varied widths
        sizes.append(n)
        non_sys.append({"role": "user", "content": ("m%02d" % i).ljust(n, ".")})
    msgs = [{"role": "system", "content": "sys"}] + non_sys
    total = sum(sizes)
    pt, cw = 80_000, 100_000             # 80% of the window -> compact
    a = StubAgent(list(msgs), pt, cw)
    stats = asyncio.run(C.maybe_compact(a))
    assert stats.get("entries") == 1, stats
    kept = [m for m in a.messages if m.get("role") != "system"]
    assert kept == non_sys[-len(kept):], "kept set must be the newest tail"
    kept_chars = sum(len(m["content"]) for m in kept)
    est = pt * kept_chars / total
    assert est <= 0.25 * cw, f"estimate {est:.0f} above the 25% target"
    k = len(non_sys) - len(kept)
    assert k >= 1, "nothing dropped despite the trigger"
    est_one_more = pt * (kept_chars + sizes[k - 1]) / total
    assert est_one_more > 0.25 * cw, \
        "one kept message more still fits — over-dropped"
    print(f"PASS token targeting: kept {len(kept)}/40 newest, "
          f"est {est:.0f} <= {0.25 * cw:.0f}")


def test_token_targeting_never_drops_the_newest():
    """Even at an unreachable target the newest message survives (the
    drop floor), and the backstop path keeps the newest 1000."""
    C._gen = _ok_gen
    non_sys = [{"role": "user", "content": ("m%02d" % i).ljust(50, ".")}
               for i in range(40)]
    msgs = [{"role": "system", "content": "sys"}] + non_sys
    a = StubAgent(list(msgs), pt=1_000_000, cw=1_000)  # target unreachable
    asyncio.run(C.maybe_compact(a))
    kept = [m for m in a.messages if m.get("role") != "system"]
    assert kept == [non_sys[-1]], "the newest message must survive the floor"
    print("PASS token targeting floor: newest message always kept")


# ---- loop: capture + persist the measurement ------------------------------

class StubCollection:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls
        self.doc = None

    async def update_one(self, q, update, **kw):
        self.calls.append((self.name, "update_one", q, update))
        if self.name == "agent_sessions":
            self.doc = dict(q, **(update.get("$set") or {}))

    async def find_one(self, q, *a, **kw):
        self.calls.append((self.name, "find_one", q))
        return self.doc

    async def insert_one(self, doc):
        self.calls.append((self.name, "insert_one", doc))

    async def insert_many(self, docs, **kw):
        self.calls.append((self.name, "insert_many", len(docs)))


class StubDB:
    def __init__(self):
        self.calls = []
        self.agent_sessions = StubCollection("agent_sessions", self.calls)
        self.agent_transcripts = StubCollection("agent_transcripts", self.calls)
        self.agents_runtime = StubCollection("agents_runtime", self.calls)
        self.client = self
        self.treegent = self


class StubSvcs:
    """Proxy double: ONE faked job result in the R78 shape — the full
    provider response (usage included) plus the job doc's stamped
    context_window."""

    def __init__(self, job_result):
        self.job_result = job_result

    async def _call(self, base, path, method="GET", body=None,
                    params=None, timeout=60.0):
        if path == "/v1/jobs":
            return {"job_id": "job1"}
        if path.startswith("/v1/jobs/"):
            return self.job_result
        raise AssertionError(f"unexpected service call: {path}")


def _make_agent(svcs, db):
    """A CENTRAL-tier Agent wired for headless turns (issue-test pattern),
    with the REAL _save_life/_load_life (the life doc is agent_sessions)."""
    a = loop.Agent.__new__(loop.Agent)
    a.name, a.id = "tester", "agt_test"
    a.actor = {"_id": "agt_test", "username": "t", "display_name": "T"}
    a.messages = [{"role": "system", "content": "sys"}]
    a.busy = False
    a._reply_ctx = None
    a._pending_senders = []
    a.last_turn_end = loop.now()
    a._life_loaded = True
    a.hc = None                      # central tier
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_central = False
    a._turn_lines = []
    a.workspace = tempfile.mkdtemp(prefix="r78_loop")
    SOUL.ensure_soul_files(a.workspace, a.actor)
    a.svcs = svcs

    async def collect_injections():
        return []
    a.collect_injections = collect_injections

    def _refresh_soul():
        pass
    a._refresh_soul = _refresh_soul
    return a


def test_loop_captures_and_persists_measurement(monkeypatch):
    """The main turn loop reads usage.prompt_tokens -> last_prompt_tokens
    and the job doc's context_window -> context_window, persists both in
    the life doc, and boot restores them alongside the messages."""
    db = StubDB()
    monkeypatch.setattr(loop, "_cdb", lambda: db)
    monkeypatch.setattr(loop, "turns", StubCollection("turns", db.calls))
    job_result = {
        "status": "done",
        "context_window": 100_000,       # stamped on the polled job doc
        "result": {"usage": {"prompt_tokens": 61_000,
                             "completion_tokens": 5,
                             "total_tokens": 61_005},
                   "choices": [{"message": {"content": "ok."}}]}}
    a = _make_agent(StubSvcs(job_result), db)
    asyncio.run(a.run_turn("heartbeat"))
    # captured from the faked job result
    assert a.last_prompt_tokens == 61_000, a.last_prompt_tokens
    assert a.context_window == 100_000, a.context_window
    # persisted in the life doc (agent_sessions $set)
    saves = [c for c in db.calls if c[0] == "agent_sessions"
             and c[1] == "update_one"]
    assert saves, "life doc must be saved"
    setdoc = saves[-1][3]["$set"]
    assert setdoc["last_prompt_tokens"] == 61_000, setdoc
    assert setdoc["context_window"] == 100_000, setdoc
    # boot restores the measurement alongside the messages
    b = _make_agent(StubSvcs(job_result), db)
    b._life_loaded = False
    asyncio.run(b._load_life())
    assert b.last_prompt_tokens == 61_000, b.last_prompt_tokens
    assert b.context_window == 100_000, b.context_window
    assert len(b.messages) == len(a.messages), b.messages
    print("PASS loop capture + life-doc persist/restore: 61000 / 100000")


def test_hosted_life_meta_roundtrip():
    """Hosted tier: the measurement persists as a sidecar next to the
    transcript; missing/corrupt meta degrades to backstop-only."""
    ws = tempfile.mkdtemp(prefix="r78_meta")
    hostclient.save_life_meta(ws, {"last_prompt_tokens": 1234,
                                   "context_window": 5678})
    assert hostclient.load_life_meta(ws) == {"last_prompt_tokens": 1234,
                                             "context_window": 5678}
    assert hostclient.load_life_meta(tempfile.mkdtemp()) == {}
    hostclient.life_meta_path(ws).write_text("{corrupt")
    assert hostclient.load_life_meta(ws) == {}
    print("PASS hosted life meta: round-trip + corrupt degrades to {}")
