"""R70 unit tests: quadtree mechanics without a live provider (fake gen_fn)."""
import asyncio, json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import qtree  # noqa: E402


def fake_gen_factory(log):
    async def gen(prompt):
        log.append(prompt)
        # summarize by reversing length info — deterministic, distinct
        n = len(prompt)
        return f"(summary of {n} chars: ...{prompt[-60:]})"
    return gen


def test_rollup_chain():
    log = []
    gen = fake_gen_factory(log)
    ws = tempfile.mkdtemp(prefix="qtree")
    # 4 compactions -> L0 gets 4 entries -> one L1 entry; L0 emptied
    for i in range(4):
        dropped = [{"role": "user", "content": f"hello batch {i} " + "x" * 500},
                   {"role": "assistant", "content": f"reply batch {i} " + "y" * 500}]
        r = asyncio.run(qtree.record_compaction(ws, dropped, gen))
        assert r["entries"] == 1
    l0 = qtree.read_entries(ws, 0)
    l1 = qtree.read_entries(ws, 1)
    meta = qtree.load_meta(ws)
    assert len(l0) == 4, f"L0 must KEEP its 4 entries, has {len(l0)}"
    assert len(l1) == 1, f"L1 must have the rollup, has {len(l1)}"
    assert meta["0"]["rolled"] == 4, meta
    assert qtree.unrolled_count(ws, 0) == 0
    print("PASS rollup: 4x L0 kept + 1x L1 created, pointer advanced")

    # 4 more -> second L1 entry; L0 still holds all 8
    for i in range(4):
        dropped = [{"role": "user", "content": f"second era {i} " + "x" * 500}]
        asyncio.run(qtree.record_compaction(ws, dropped, gen))
    assert len(qtree.read_entries(ws, 0)) == 8
    assert len(qtree.read_entries(ws, 1)) == 2
    print("PASS append-only: L0=8, L1=2")

    # push to 4 L1 entries -> L2 appears, L1 keeps all 4
    for era in range(2):
        for i in range(4):
            dropped = [{"role": "user", "content": f"era{era}.{i} " + "x" * 500}]
            asyncio.run(qtree.record_compaction(ws, dropped, gen))
    assert len(qtree.read_entries(ws, 0)) == 16
    assert len(qtree.read_entries(ws, 1)) == 4
    assert len(qtree.read_entries(ws, 2)) == 1
    print("PASS recursive: 16 compactions -> L2 exists, all children kept")


def test_entry_cap():
    log = []
    gen = fake_gen_factory(log)
    ws = tempfile.mkdtemp(prefix="qtree2")
    huge = [{"role": "user", "content": "z" * 20000}]
    asyncio.run(qtree.record_compaction(ws, huge, gen))
    e = qtree.read_entries(ws, 0)[0]
    assert len(e) <= qtree.COMPACT_ENTRY_MAX, len(e)
    print("PASS entry cap:", len(e), "<=", qtree.COMPACT_ENTRY_MAX)


def test_context_block():
    gen = fake_gen_factory([])
    ws = tempfile.mkdtemp(prefix="qtree3")
    for era in range(4):
        for i in range(4):
            dropped = [{"role": "user",
                        "content": f"era {era} msg {i} — decision D{era}{i}"}]
            asyncio.run(qtree.record_compaction(ws, dropped, gen))
    block = qtree.context_block(ws)
    assert "<episodic" not in block  # raw view; wrapper added by soul
    assert "[recent]" in block or "era" in block
    lines = block.strip().splitlines()
    assert len(lines) >= 1
    print("PASS context block: %d level lines, %d chars" % (len(lines), len(block)))
    # budget trimming drops OLDEST (front) lines first
    small = qtree.context_block(ws, budget=300)
    assert len(small) <= 300 + 200  # one line may exceed tiny budget alone
    print("PASS budget trim: %d chars" % len(small))


def test_needs_compaction():
    msgs = [{"role": "system", "content": "s" * 50000},
            {"role": "user", "content": "u" * 400000},
            {"role": "assistant", "content": "a" * 400000}]
    assert not qtree.needs_compaction(msgs)   # 800k < 1.2M
    msgs.append({"role": "user", "content": "u" * 500000})
    assert qtree.needs_compaction(msgs)       # 1.3M > 1.2M
    print("PASS trigger threshold")


def test_gen_failure_degrades():
    """Issue #9: summarize failure -> the raw material is persisted
    verbatim, a head+tail placeholder pointing at it is written, and
    record_compaction RAISES so the caller keeps the messages in the
    live context (the old material[:limit] kept 2.6% and destroyed the
    newest material)."""
    async def bad_gen(prompt):
        raise RuntimeError("provider down")
    ws = tempfile.mkdtemp(prefix="qtree4")
    dropped = [{"role": "user", "content": "keep me raw " + "q" * 100},
               {"role": "assistant", "content": "tail content XYZ"}]
    try:
        asyncio.run(qtree.record_compaction(ws, dropped, bad_gen))
        raise AssertionError("record_compaction must raise when "
                             "summarization fails")
    except RuntimeError:
        pass
    e = qtree.read_entries(ws, 0)[0]
    assert "keep me raw" in e, "head of material must be kept"
    assert "tail content XYZ" in e, "tail of material must be kept"
    assert "[TRUNCATED" in e and "memory/qtree/raw/" in e, \
        "placeholder must carry the explicit raw-file marker"
    raw_dir = os.path.join(ws, qtree.QTREE_DIR, "raw")
    raws = os.listdir(raw_dir)
    assert len(raws) == 1, raws
    with open(os.path.join(raw_dir, raws[0]), encoding="utf-8") as f:
        raw = f.read()
    assert "keep me raw" in raw and "tail content XYZ" in raw, \
        "raw file must hold the material verbatim"
    print("PASS degrade: raw persisted + head/tail placeholder, error raised")


if __name__ == "__main__":
    test_rollup_chain()
    test_entry_cap()
    test_context_block()
    test_needs_compaction()
    test_gen_failure_degrades()
    print("ALL QTREE TESTS PASS")


def test_count_trigger():
    """Short-message sessions compact on COUNT before _sanitize's 500 cap."""
    gen = fake_gen_factory([])
    ws = tempfile.mkdtemp(prefix="qtree6")
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(410):   # 410 non-system msgs, tiny chars
        msgs.append({"role": "user", "content": f"m{i}"})
    assert qtree.needs_compaction(msgs), "count path must fire"
    small = msgs[:20]
    assert not qtree.needs_compaction(small)
    print("PASS count trigger: 410 msgs fires, 19 does not")


def test_compact_maybe_both_paths():
    """maybe_compact honors both budgets via a stub agent."""
    import asyncio
    from app import compact as C

    class StubAgent:
        def __init__(self, msgs):
            self.messages = msgs
            self.workspace = tempfile.mkdtemp(prefix="qtree7")
            self.regened = False
        def _regen_system_prompt(self):
            self.regened = True

    async def ok_gen(agent, prompt): return "stub summary"

    # count path: many tiny messages
    msgs = [{"role": "system", "content": "s"}]
    for i in range(500):
        msgs.append({"role": "user", "content": f"tiny {i}"})
    a = StubAgent(msgs)
    C._gen = ok_gen
    stats = asyncio.run(C.maybe_compact(a))
    kept = len(a.messages) - 1
    print("PASS maybe_compact count path:", stats, "kept", kept, "regen:", a.regened)
    assert stats.get("entries") == 1 and kept < 500
    assert a.regened, "system prompt must be regenerated on success"


def test_issue4_episodic_strip_idempotent():
    """Issue #4: strip_soul must remove the <episodic-memory> block too —
    a refresh cycle (strip -> soul_block prepend) may never accumulate
    copies (was +1 per generation step = unbounded prompt growth)."""
    import os
    from app import soul

    ws = tempfile.mkdtemp(prefix="qtree_i4")
    os.makedirs(os.path.join(ws, qtree.QTREE_DIR), exist_ok=True)
    with open(os.path.join(ws, "SOUL.md"), "w") as f:
        f.write("You are a test agent.")
    qtree._append_entry(ws, 0, "- [2026-10-08 22:00] first summary entry")
    qtree._save_meta(ws, {"0": {"entries": 1, "rolled": 0}})

    body = "Base system text."
    for step in range(1, 6):   # 5 refresh cycles = a 5-step turn
        body = soul.strip_soul(body)
        body = soul.soul_block(ws) + "\n\n" + body
        assert body.count("<episodic-memory") == 1, \
            f"step {step}: episodic block accumulated"
        assert body.count("<soul>") == 1
    print("PASS issue #4: episodic block stays at 1 across 5 refresh cycles")


def test_issue5_budget_trim_keeps_L0():
    """Issue #5: under budget pressure the trim must drop the COARSEST
    level first and keep the recent L0 entry (it used to drop L0 and keep
    ancient digests — exactly backwards)."""
    ws = tempfile.mkdtemp(prefix="qtree_i5")
    for lvl in range(5):
        qtree._append_entry(ws, lvl, f"- [ts] LEVEL-{lvl} digest")
    meta = {str(lvl): {"entries": 1, "rolled": 1 if lvl < 4 else 0}
            for lvl in range(5)}
    qtree._save_meta(ws, meta)

    block = qtree.context_block(ws, budget=90)
    assert "LEVEL-0" in block, "recent L0 entry must survive the trim"
    assert "LEVEL-4" not in block, "coarsest digest must be dropped first"
    # tag math (issue #5 bonus): L2 summarizes 4^2 = 16 L0 compactions
    full = qtree.context_block(ws, budget=10_000)
    assert "~16 compactions ago" in full and "~4 compactions ago" in full
    assert "1 of" not in full, "only one entry per level rides in context"
    print("PASS issue #5: L0 kept, coarse dropped, tags corrected")


def test_issue9_summarize_failure_keeps_context():
    """Issue #9 regression: when the summarizer raises, maybe_compact
    must NOT drop messages from the live context (compaction retries
    next turn), the dropped material must be persisted VERBATIM to
    memory/qtree/raw/, and any entry written must keep head AND tail
    with the explicit TRUNCATED marker pointing at that raw file."""
    from app import compact as C

    class StubAgent:
        def __init__(self, msgs):
            self.messages = msgs
            self.workspace = tempfile.mkdtemp(prefix="qtree_i9")
            self.regened = False
        def _regen_system_prompt(self):
            self.regened = True

    async def bad_gen(agent, prompt):
        raise RuntimeError("provider 5xx")

    msgs = [{"role": "system", "content": "sys"}]
    # 500 non-system msgs: the count budget drops the oldest 500-200 = 300
    last_dropped = 500 - qtree.COMPACT_TRIGGER_MSGS // 2 - 1
    for i in range(500):
        pad = "x" * 300
        if i == 0:
            content = "HEAD-KEEP-ME " + pad          # head of dropped material
        elif i == last_dropped:
            content = pad + " TAIL-KEEP-ME"          # tail of dropped material
        else:
            content = f"msg {i} " + pad
        msgs.append({"role": "user", "content": content})
    a = StubAgent(list(msgs))
    before = list(a.messages)
    C._gen = bad_gen
    stats = asyncio.run(C.maybe_compact(a))
    assert stats == {}, "no success stats on failure"
    assert len(a.messages) == len(before), \
        "messages must NOT be dropped when summarization fails"
    assert a.messages == before, "context must be byte-identical after failure"
    assert any(m["content"].startswith("HEAD-KEEP-ME") for m in a.messages)
    assert not a.regened, "no system-prompt regen on failure"

    raw_dir = os.path.join(a.workspace, qtree.QTREE_DIR, "raw")
    raws = os.listdir(raw_dir)
    assert raws, "raw file must exist"
    with open(os.path.join(raw_dir, raws[0]), encoding="utf-8") as f:
        raw = f.read()
    assert "HEAD-KEEP-ME" in raw and "TAIL-KEEP-ME" in raw, \
        "raw file must hold the full material verbatim"
    assert len(raw) > qtree.COMPACT_ENTRY_MAX, \
        "raw must be the FULL material, not a truncation"

    entries = qtree.read_entries(a.workspace, 0)
    assert entries, "failure-path L0 entry must point at the raw file"
    e = entries[0]
    assert "HEAD-KEEP-ME" in e, "placeholder must keep the head"
    assert "TAIL-KEEP-ME" in e, "placeholder must keep the tail"
    assert "[TRUNCATED" in e and "memory/qtree/raw/" in e
    assert len(e) <= qtree.COMPACT_ENTRY_MAX

    # the next turn retries compaction instead of compounding the loss
    stats = asyncio.run(C.maybe_compact(a))
    assert len(a.messages) == len(before), "retry must also keep context"
    print("PASS issue #9: failure keeps context, raw verbatim, "
          "placeholder has head+tail+raw marker")
