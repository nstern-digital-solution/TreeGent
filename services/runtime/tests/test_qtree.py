"""R70 unit tests: quadtree mechanics without a live provider (fake gen_fn)."""
import asyncio, json, os, re, sys, tempfile

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
    """R75: the block carries EVERY unrolled entry of every level — the
    unrolled sets across levels partition the whole compacted life
    (gapless). 16 compactions at one era roll up completely: L0 fully
    rolled, L1 fully rolled, only L2's single unrolled entry rides."""
    gen = fake_gen_factory([])
    ws = tempfile.mkdtemp(prefix="qtree3")
    for era in range(4):
        for i in range(4):
            dropped = [{"role": "user",
                        "content": f"era {era} msg {i} — decision D{era}{i}"}]
            asyncio.run(qtree.record_compaction(ws, dropped, gen))
    meta = qtree.load_meta(ws)
    assert meta["0"] == {"entries": 16, "rolled": 16}, meta
    assert meta["1"] == {"entries": 4, "rolled": 4}, meta
    assert meta["2"] == {"entries": 1, "rolled": 0}, meta
    block = qtree.context_block(ws)
    assert "<episodic" not in block  # raw view; wrapper added by soul
    l0 = qtree.read_entries(ws, 0)
    l1 = qtree.read_entries(ws, 1)
    l2 = qtree.read_entries(ws, 2)
    l2_body = re.sub(r"^- \[[^\]]+\] ", "", l2[0])
    lines = block.strip().splitlines()
    # EXACTLY the unrolled sets: no L0 line, no L1 line, one L2 line
    assert lines == [f"[~16 compactions ago] {l2_body}"], lines
    for entries, level in ((l0, 0), (l1, 1)):
        for e in entries:
            body = re.sub(r"^- \[[^\]]+\] ", "", e)
            tag = "recent" if level == 0 else f"~{4 ** level} compactions ago"
            assert f"[{tag}] {body}" not in lines, \
                f"rolled L{level} entry must not ride"
    print("PASS context block (16 compactions): 1 L2 line, no L0/L1 lines")

    # unrolled remainder: 6 L0 entries (rolled=4) + 1 L1 ->
    # block = L0[4], L0[5], L1[0] and nothing else
    ws2 = tempfile.mkdtemp(prefix="qtree3b")
    for i in range(6):
        dropped = [{"role": "user", "content": f"batch {i} — decision E{i}"}]
        asyncio.run(qtree.record_compaction(ws2, dropped, gen))
    meta = qtree.load_meta(ws2)
    assert meta["0"] == {"entries": 6, "rolled": 4}, meta
    assert meta["1"] == {"entries": 1, "rolled": 0}, meta
    l0 = qtree.read_entries(ws2, 0)
    l1 = qtree.read_entries(ws2, 1)
    strip = lambda e: re.sub(r"^- \[[^\]]+\] ", "", e)  # noqa: E731
    expected = [f"[recent] {strip(l0[4])}",
                f"[recent] {strip(l0[5])}",
                f"[~4 compactions ago] {strip(l1[0])}"]
    assert qtree.context_block(ws2).strip().splitlines() == expected
    print("PASS context block (remainder): L0[4], L0[5], L1[0] — exact")


def test_needs_compaction():
    """R78 trigger: strictly > 50% of the context window (0.50x exactly
    does NOT fire); the old char trigger is retired."""
    few = [{"role": "system", "content": "s"},
           {"role": "user", "content": "u" * 400000}]
    assert not qtree.needs_compaction(few, 49_000, 100_000)   # 0.49x
    assert not qtree.needs_compaction(few, 50_000, 100_000)   # 0.50x exactly
    assert qtree.needs_compaction(few, 51_000, 100_000)       # 0.51x
    print("PASS trigger threshold: 0.49x/0.50x no, 0.51x yes")


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
    """R78 backstop: the message COUNT fires at 2000 (guard only) — and
    chars are irrelevant: a 10M-char log with tiny tokens and few
    messages must NOT compact (the retired char trigger would have)."""
    def msgs(n):
        return [{"role": "system", "content": "sys"}] + \
               [{"role": "user", "content": f"m{i}"} for i in range(n)]
    assert not qtree.needs_compaction(msgs(1999)), "1999 must not fire"
    assert qtree.needs_compaction(msgs(2000)), "2000 must fire"
    assert qtree.needs_compaction(msgs(2001)), "2001 must fire"
    # char-count irrelevance
    huge = [{"role": "system", "content": "s"},
            {"role": "user", "content": "x" * 5_000_000},
            {"role": "assistant", "content": "y" * 5_000_000}]
    assert not qtree.needs_compaction(huge, 100, 100_000), \
        "10M chars + tiny tokens + few msgs = NO compaction"
    print("PASS count backstop: 1999 no / 2000-2001 yes; chars irrelevant")


def test_compact_maybe_both_paths():
    """maybe_compact drives both R78 paths via a stub agent: the token
    path drops to the 25% landing zone, the backstop path keeps the
    newest 1000."""
    from app import compact as C

    class StubAgent:
        def __init__(self, msgs, pt=0, cw=0):
            self.messages = msgs
            self.last_prompt_tokens = pt
            self.context_window = cw
            self.workspace = tempfile.mkdtemp(prefix="qtree7")
            self.regened = False
        def _regen_system_prompt(self):
            self.regened = True

    async def ok_gen(agent, prompt): return "stub summary"

    # token path: 500 uniform 10-char messages, 62.4% of the window used
    # -> exactly 300 oldest dropped (est lands at 24960 <= 25000)
    msgs = [{"role": "system", "content": "s"}]
    for i in range(500):
        msgs.append({"role": "user", "content": f"filler {i:03d}"})
    a = StubAgent(msgs, pt=62_400, cw=100_000)
    C._gen = ok_gen
    stats = asyncio.run(C.maybe_compact(a))
    kept = len(a.messages) - 1
    assert stats.get("entries") == 1 and kept == 200, (stats, kept)
    assert a.regened, "system prompt must be regenerated on success"
    # backstop path: 2100 tiny messages, no usage data -> keep newest 1000
    msgs = [{"role": "system", "content": "s"}]
    for i in range(2100):
        msgs.append({"role": "user", "content": f"tiny {i}"})
    a = StubAgent(msgs)
    stats = asyncio.run(C.maybe_compact(a))
    kept = len(a.messages) - 1
    assert stats.get("entries") == 1 and kept == 1000, (stats, kept)
    print("PASS maybe_compact: token path keeps 200/500, backstop keeps "
          "1000/2100, regen:", a.regened)


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
    """Issue #5 + R75: only UNROLLED entries ride (a fully rolled level
    contributes 0 lines); under budget pressure the trim still drops the
    COARSEST material first — a level's OLDEST unrolled entries before its
    newest — and the recent L0 entry is the last thing to go."""
    ws = tempfile.mkdtemp(prefix="qtree_i5")
    # 5 levels, one entry each, all rolled except L4 -> only L4 is unrolled
    for lvl in range(5):
        qtree._append_entry(ws, lvl, f"- [ts] LEVEL-{lvl} digest")
    meta = {str(lvl): {"entries": 1, "rolled": 1 if lvl < 4 else 0}
            for lvl in range(5)}
    qtree._save_meta(ws, meta)

    # R75: unrolled-only entries ride — the block is EXACTLY the L4 entry
    full = qtree.context_block(ws)
    assert full.strip().splitlines() == \
        ["[~256 compactions ago] LEVEL-4 digest"], full
    for lvl in range(4):
        assert f"LEVEL-{lvl} " not in full, \
            f"fully rolled L{lvl} must not ride (R75: unrolled only)"

    # multi-level state: 3 unrolled L0 + 2 L1 + 1 L2 + 1 L3
    ws2 = tempfile.mkdtemp(prefix="qtree_i5b")
    spec = {0: (7, 4), 1: (4, 2), 2: (2, 1), 3: (1, 0)}
    names = {}
    for lvl, (n_entries, _rolled) in spec.items():
        for idx in range(n_entries):
            name = f"L{lvl}-{idx}-" + "z" * 12      # equal-length bodies
            qtree._append_entry(ws2, lvl, f"- [ts] {name}")
            names[(lvl, idx)] = name
    qtree._save_meta(ws2, {str(l): {"entries": n, "rolled": r}
                           for l, (n, r) in spec.items()})

    def tag(level):
        return "recent" if level == 0 else f"~{4 ** level} compactions ago"
    # display order: fine->coarse, oldest first within a level
    expected = [f"[{tag(l)}] {names[(l, i)]}"
                for l in (0, 1, 2, 3)
                for i in range(spec[l][1], spec[l][0])]
    # = L0[4], L0[5], L0[6], L1[2], L1[3], L2[1], L3[0]
    full = qtree.context_block(ws2, budget=10_000)
    assert full.strip().splitlines() == expected, full
    assert full.count("[recent]") == 3, \
        "all 3 unrolled L0 entries must ride (opposite of the old '1 of')"
    # tag math (issue #5 bonus): L2 summarizes 4^2 = 16 L0 compactions
    assert "~16 compactions ago" in full and "~4 compactions ago" in full

    def joined(idxs):
        return "\n".join(expected[i] for i in idxs)
    # drop order: L3[0], L2[1], L1[2], L1[3], L0[4], L0[5] -> survivor L0[6]
    # one drop: the coarsest level's entry goes first
    b = len(joined([0, 1, 2, 3, 4, 5]))
    assert qtree.context_block(ws2, budget=b) == joined([0, 1, 2, 3, 4, 5])
    # two drops: coarsest level fully gone before the next is touched
    b = len(joined([0, 1, 2, 3, 4]))
    assert qtree.context_block(ws2, budget=b) == joined([0, 1, 2, 3, 4])
    # three drops: within a level the OLDEST unrolled entry goes first
    # (L1[2] dropped, L1[3] kept)
    b = len(joined([0, 1, 2, 4]))
    assert qtree.context_block(ws2, budget=b) == joined([0, 1, 2, 4])
    # five drops: only the two newest L0 entries remain
    b = len(joined([1, 2]))
    assert qtree.context_block(ws2, budget=b) == joined([1, 2])
    # tiny budget: the newest L0 entry is the last thing to go (issue #5)
    assert qtree.context_block(ws2, budget=1) == expected[2]
    print("PASS issue #5/R75: unrolled-only rides, trim drops coarsest/oldest"
          " first, newest L0 survives")


def test_issue9_summarize_failure_keeps_context():
    """Issue #9 regression: when the summarizer raises, maybe_compact
    must NOT drop messages from the live context (compaction retries
    next turn), the dropped material must be persisted VERBATIM to
    memory/qtree/raw/, and any entry written must keep head AND tail
    with the explicit TRUNCATED marker pointing at that raw file."""
    from app import compact as C

    class StubAgent:
        def __init__(self, msgs, pt=0, cw=0):
            self.messages = msgs
            self.last_prompt_tokens = pt
            self.context_window = cw
            self.workspace = tempfile.mkdtemp(prefix="qtree_i9")
            self.regened = False
        def _regen_system_prompt(self):
            self.regened = True

    async def bad_gen(agent, prompt):
        raise RuntimeError("provider 5xx")

    msgs = [{"role": "system", "content": "sys"}]
    # 500 uniform 400-char messages: the token path (62.4% of a 100k
    # window) drops the oldest 300 — HEAD at 0, TAIL at the LAST dropped
    # (299). Uniform width makes the char-proportion estimate exact.
    for i in range(500):
        if i == 0:
            content = "HEAD-KEEP-ME ".ljust(400, "x")
        elif i == 299:
            content = ("x" * 200 + " TAIL-KEEP-ME").ljust(400, "x")
        else:
            content = f"msg {i} ".ljust(400, "x")
        msgs.append({"role": "user", "content": content})
    a = StubAgent(list(msgs), pt=62_400, cw=100_000)
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


def test_r75_gapless_partition():
    """R75: 21 compactions (21 = 5*4+1) -> the unrolled tails of all levels
    PARTITION the whole compacted life — gapless coverage of all 21 eras
    with one line each: L0[20], L1[4], L2[0]. The 22nd compaction (no
    rollup) grows the block to 2 L0 lines without touching L1/L2."""
    log = []
    gen = fake_gen_factory(log)
    ws = tempfile.mkdtemp(prefix="qtree_r75")
    strip = lambda e: re.sub(r"^- \[[^\]]+\] ", "", e)  # noqa: E731

    def drop(i):
        return [{"role": "user",
                 "content": f"compaction {i:02d} payload — event E{i:02d} "
                            + "x" * 120}]

    for i in range(21):
        asyncio.run(qtree.record_compaction(ws, drop(i), gen))
    meta = qtree.load_meta(ws)
    assert meta["0"] == {"entries": 21, "rolled": 20}, meta
    assert meta["1"] == {"entries": 5, "rolled": 4}, meta
    assert meta["2"] == {"entries": 1, "rolled": 0}, meta

    block = qtree.context_block(ws)
    l0 = qtree.read_entries(ws, 0)
    l1 = qtree.read_entries(ws, 1)
    l2 = qtree.read_entries(ws, 2)
    expected = [f"[recent] {strip(l0[20])}",
                f"[~4 compactions ago] {strip(l1[4])}",
                f"[~16 compactions ago] {strip(l2[0])}"]
    lines = block.strip().splitlines()
    assert lines == expected, lines   # EXACTLY L0[20], L1[4], L2[0]
    # gapless coverage: the unrolled sets partition all 21 compactions
    # (each unrolled Ln entry covers 4^n L0 eras: 1 + 4 + 16 = 21)
    covered = sum((meta[str(l)]["entries"] - meta[str(l)]["rolled"]) * 4 ** l
                  for l in (0, 1, 2))
    assert covered == 21, covered
    # ≤3 lines per level and ≤3*4000 chars per level (fanout-4 construction)
    per_level = {0: lines[0:1], 1: lines[1:2], 2: lines[2:3]}
    for lvl, seg in per_level.items():
        assert len(seg) <= 3, f"L{lvl}: {len(seg)} lines"
        assert sum(len(x) for x in seg) <= 3 * qtree.COMPACT_ENTRY_MAX
    print("PASS r75 gapless (21 compactions):")
    print(block)

    # 22nd compaction: no rollup -> 2 L0 lines, L1/L2 lines unchanged
    asyncio.run(qtree.record_compaction(ws, drop(21), gen))
    meta = qtree.load_meta(ws)
    assert meta["0"] == {"entries": 22, "rolled": 20}, meta
    assert meta["1"] == {"entries": 5, "rolled": 4}, meta
    assert meta["2"] == {"entries": 1, "rolled": 0}, meta
    l0 = qtree.read_entries(ws, 0)
    lines2 = qtree.context_block(ws).strip().splitlines()
    assert lines2 == [f"[recent] {strip(l0[20])}",
                      f"[recent] {strip(l0[21])}",
                      f"[~4 compactions ago] {strip(l1[4])}",
                      f"[~16 compactions ago] {strip(l2[0])}"], lines2
    assert lines2[2:] == lines[1:], "L1/L2 lines must be unchanged"
    assert sum(len(x) for x in lines2[:2]) <= 3 * qtree.COMPACT_ENTRY_MAX
    print("PASS r75 gapless (22nd compaction): 2 L0 lines, L1/L2 unchanged")
