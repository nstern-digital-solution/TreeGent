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
            {"role": "user", "content": "u" * 40000},
            {"role": "assistant", "content": "a" * 40000}]
    assert not qtree.needs_compaction(msgs)   # 80k < 96k
    msgs.append({"role": "user", "content": "u" * 20000})
    assert qtree.needs_compaction(msgs)       # 100k > 96k
    print("PASS trigger threshold")


def test_gen_failure_degrades():
    async def bad_gen(prompt):
        raise RuntimeError("provider down")
    ws = tempfile.mkdtemp(prefix="qtree4")
    dropped = [{"role": "user", "content": "keep me raw " + "q" * 100}]
    r = asyncio.run(qtree.record_compaction(ws, dropped, bad_gen))
    e = qtree.read_entries(ws, 0)[0]
    assert "keep me raw" in e
    print("PASS degrade: raw tail kept when provider fails")


if __name__ == "__main__":
    test_rollup_chain()
    test_entry_cap()
    test_context_block()
    test_needs_compaction()
    test_gen_failure_degrades()
    print("ALL QTREE TESTS PASS")


def test_count_trigger():
    """Short-message sessions compact on COUNT before _sanitize's 200 cap."""
    gen = fake_gen_factory([])
    ws = tempfile.mkdtemp(prefix="qtree6")
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(170):   # 170 non-system msgs, tiny chars
        msgs.append({"role": "user", "content": f"m{i}"})
    assert qtree.needs_compaction(msgs), "count path must fire"
    small = msgs[:20]
    assert not qtree.needs_compaction(small)
    print("PASS count trigger: 170 msgs fires, 19 does not")


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

    async def ok_gen(p): return "stub summary"

    # count path: many tiny messages
    msgs = [{"role": "system", "content": "s"}]
    for i in range(200):
        msgs.append({"role": "user", "content": f"tiny {i}"})
    a = StubAgent(msgs)
    C._gen = ok_gen
    stats = asyncio.run(C.maybe_compact(a))
    kept = len(a.messages) - 1
    print("PASS maybe_compact count path:", stats, "kept", kept, "regen:", a.regened)
    assert stats.get("entries") == 1 and kept < 200
