"""R76 unit tests: derived-memory layer (deriver + store + vector search +
dreamer) without a live provider (fake gen/embed functions).

Covers per the R76 spec:
- two-output compaction prompt (summary + conclusions) parseable;
  old-format outputs (no section) still compact (back-compat).
- conclusions appended WITH embeddings when the embed fn works and
  WITHOUT the field when it raises (degraded); compaction NEVER raises
  from the conclusions side.
- memory.search: cosine ranking with known vectors -> known order,
  top-k formatting, k clamp, notes results labeled (source: notes),
  degraded "(vector memory unavailable)" note.
- dreamer cadence table + atomic apply + failure leaves state untouched.
"""
import asyncio, json, os, sys, tempfile  # noqa: E401
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import conclusions, dreamer, qtree  # noqa: E402
from app.tools import ToolContext, t_memory_search  # noqa: E402

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

DROPPED = [{"role": "user", "content": "hello there " + "x" * 200},
           {"role": "assistant", "content": "reply " + "y" * 200}]


# ---------------- helpers ----------------

def two_output_gen(summary="Dense summary of the era."):
    """Fake compaction generation producing the NEW two-output format."""
    async def gen(prompt):
        return (f"{summary}\n===CONCLUSIONS===\n"
                "CONCLUSION: Alice prefers async status updates. || "
                "KIND: peer:alice || FROM: msg 2, \"async\"\n"
                "CONCLUSION: The proxy listens on port 8010. || "
                "KIND: state || FROM: quote \"8010\"\n")
    return gen


def ok_embed():
    async def emb(texts):
        return [[1.0, 0.0] for _ in texts]
    return emb


def bad_embed():
    async def emb(texts):
        raise RuntimeError("no embedding model")
    return emb


def write_rows(ws, rows):
    conclusions.append_records(ws, rows)


def mk(text, emb=None, kind="task", era="L0:1", ts=NOW.isoformat(),
       superseded_by=None, premises=None):
    return conclusions.make_record(
        text=text, kind=kind, era=era, premises=premises or [],
        ts=ts, embedding=emb, superseded_by=superseded_by)


# ---------------- 1. prompt / parsing ----------------

def test_prompt_two_outputs_parseable():
    out = ("Dense summary line one.\nline two.\n"
           "===CONCLUSIONS===\n"
           "CONCLUSION: Alice prefers async updates. || KIND: peer:alice || "
           "FROM: msg 2\n"
           "CONCLUSION: Deploy runs on port 8010. || KIND: state || "
           "FROM: quote \"8010\"\n")
    summary, concl = qtree.parse_generation(out)
    assert summary == "Dense summary line one.\nline two.", summary
    assert len(concl) == 2, concl
    assert concl[0]["kind"] == "peer:alice"
    assert concl[0]["text"] == "Alice prefers async updates."
    assert concl[0]["premises"] == ["msg 2"]
    assert concl[1]["kind"] == "state"
    # malformed kind falls back to task; missing FROM -> empty premises
    s2, c2 = qtree.parse_generation(
        "sum\n===CONCLUSIONS===\nCONCLUSION: no kind here\n"
        "CONCLUSION: K || KIND: bogus-kind || FROM: a; b\n")
    assert s2 == "sum"
    assert c2[0]["kind"] == "task" and c2[0]["premises"] == []
    assert c2[1]["kind"] == "task" and c2[1]["premises"] == ["a", "b"]
    # caps: max 12 conclusions per pass, text <= 400 chars
    lines = "\n".join(f"CONCLUSION: text number {i} || KIND: task || FROM: x"
                      for i in range(20))
    _, c3 = qtree.parse_generation(
        f"sum\n===CONCLUSIONS===\n{lines}\n"
        f"CONCLUSION: {'z' * 900} || KIND: task || FROM: x\n")
    assert len(c3) == 12, len(c3)
    _, c4 = qtree.parse_generation(
        "sum\n===CONCLUSIONS===\nCONCLUSION: " + "z" * 900 +
        " || KIND: task || FROM: x\n")
    assert len(c4[0]["text"]) <= conclusions.CONCLUSION_TEXT_MAX
    print("PASS prompt parsing: kinds, premises, 12-cap, 400-char cap")


def test_old_format_backcompat():
    """A summary WITHOUT a conclusions section compacts exactly as before
    and derives nothing."""
    async def gen(prompt):
        return f"(summary of {len(prompt)} chars)"
    ws = tempfile.mkdtemp(prefix="r76a")
    r = asyncio.run(qtree.record_compaction(ws, DROPPED, gen))
    assert r["entries"] == 1
    assert r["conclusions"] == 0
    entries = qtree.read_entries(ws, 0)
    assert len(entries) == 1 and entries[0].startswith("- [")
    assert "(summary of" in entries[0]
    assert conclusions.load_conclusions(ws) == []
    # marker inside the SUMMARY text must not leak into the entry
    async def gen2(prompt):
        return ("plain summary\n===CONCLUSIONS===\n"
                "CONCLUSION: kept || KIND: task || FROM: x")
    ws2 = tempfile.mkdtemp(prefix="r76b")
    asyncio.run(qtree.record_compaction(ws2, DROPPED, gen2))
    e = qtree.read_entries(ws2, 0)[0]
    assert "===CONCLUSIONS===" not in e, e
    assert "plain summary" in e
    print("PASS back-compat: old-format output compacts, nothing derived")


# ---------------- 2. conclusions storage ----------------

def test_conclusions_appended_with_embeddings():
    ws = tempfile.mkdtemp(prefix="r76c")
    r = asyncio.run(qtree.record_compaction(ws, DROPPED, two_output_gen(),
                                            embed_fn=ok_embed()))
    assert r["conclusions"] == 2, r
    recs = conclusions.load_conclusions(ws)
    assert len(recs) == 2, recs
    for rec in recs:
        assert rec["embedding"] == [1.0, 0.0], rec
        assert rec["id"].startswith("cnc_")
        assert rec["era"] == "L0:1", rec
        assert rec["kind"] in ("peer:alice", "state")
        assert rec["superseded_by"] is None
        # premises carry the model refs AND the raw source file
        assert any(p.startswith("raw/") for p in rec["premises"]), rec
    assert recs[0]["kind"] == "peer:alice"
    assert recs[0]["ts"] == recs[1]["ts"]
    print("PASS conclusions stored with embeddings, era + raw premise")


def test_conclusions_degraded_without_embeddings():
    """Embed fn raises -> rows land WITHOUT the embedding field and the
    compaction still succeeds (degraded, never broken)."""
    ws = tempfile.mkdtemp(prefix="r76d")
    r = asyncio.run(qtree.record_compaction(ws, DROPPED, two_output_gen(),
                                            embed_fn=bad_embed()))
    assert r["entries"] == 1
    assert r["conclusions"] == 2
    assert r["conclusions_unembedded"] == 2, r
    recs = conclusions.load_conclusions(ws)
    assert len(recs) == 2
    for rec in recs:
        assert "embedding" not in rec, rec
    # the summary itself was saved normally
    assert "Dense summary of the era." in qtree.read_entries(ws, 0)[0]
    # search skips unembedded rows instead of erroring
    assert conclusions.search_conclusions(ws, [1.0, 0.0], k=5) == []
    print("PASS degraded: no embedding field, compaction unharmed")


def test_compaction_never_fails_from_conclusions():
    """Even a completely broken conclusions side (store unwritable AND
    embed fn exploding) must not fail the compaction."""
    ws = tempfile.mkdtemp(prefix="r76e")
    # make the conclusions file path a DIRECTORY -> append must fail
    os.makedirs(os.path.join(ws, "memory", "conclusions.jsonl"))
    r = asyncio.run(qtree.record_compaction(ws, DROPPED, two_output_gen(),
                                            embed_fn=bad_embed()))
    assert r["entries"] == 1, r
    assert qtree.read_entries(ws, 0), "summary entry must exist"
    print("PASS never-raise: unwritable store + dead embed -> compaction OK")


# ---------------- 3. search ----------------

def test_search_ranks_by_cosine():
    """Known vectors -> known order (cosine with query [1, 0])."""
    ws = tempfile.mkdtemp(prefix="r76f")
    write_rows(ws, [
        mk("A aligned", emb=[1.0, 0.0], era="L0:1"),
        mk("B diagonal", emb=[0.7, 0.7], era="L0:2"),
        mk("C orthogonal", emb=[0.0, 1.0], era="L0:3"),
        mk("D unembedded", emb=None, era="L0:4"),
    ])
    hits = conclusions.search_conclusions(ws, [1.0, 0.0], k=3)
    assert [h["text"] for h in hits] == ["A aligned", "B diagonal",
                                         "C orthogonal"], hits
    assert len(conclusions.search_conclusions(ws, [1.0, 0.0], k=1)) == 1
    # superseded rows never resurface
    write_rows(ws, [mk("A2 replaces A", emb=[1.0, 0.0], era="L0:5")])
    rows = conclusions.load_conclusions(ws)
    conclusions.apply_dream(ws, {rows[0]["id"]: rows[4]["id"]}, [])
    hits = conclusions.search_conclusions(ws, [1.0, 0.0], k=5)
    assert [h["text"] for h in hits][0] == "A2 replaces A", hits
    assert all(h["text"] != "A aligned" for h in hits)
    print("PASS cosine rank: known order, unembedded + superseded skipped")


def _stub_ctx(ws, vec=(1.0, 0.0), raise_=False):
    class StubSvcs:
        def __init__(self):
            self.calls = []
        async def _call(self, base, path, method="GET", body=None,
                        params=None, timeout=60.0):
            self.calls.append((path, body))
            if raise_:
                raise RuntimeError("service 503: no embedding model")
            return {"data": [{"embedding": list(vec), "index": 0}]}
    svcs = StubSvcs()
    return ToolContext("agt_test", ws, svcs), svcs


def test_memory_search_format_and_k_clamp():
    ws = tempfile.mkdtemp(prefix="r76g")
    rows = [mk(f"conclusion {i}", emb=[1.0, 0.0], kind="peer:alice",
               era=f"L0:{21 + i}", premises=["raw/x.md"])
            for i in range(25)]
    write_rows(ws, rows)
    with open(os.path.join(ws, "MEMORY.md"), "w") as f:
        f.write("# Memory\n\n- [2026-10-11 10:00] needle in notes\n")
    ctx, svcs = _stub_ctx(ws)

    out = asyncio.run(t_memory_search(ctx, {"q": "needle", "k": 3}))
    lines = out.splitlines()
    vec_lines = [l for l in lines if l.startswith("  [")]
    assert len(vec_lines) == 3, lines
    assert vec_lines[0] == ("  [peer:alice, era L0:21] conclusion 0 "
                           "(premises: raw/x.md)"), vec_lines[0]
    # the query went to the embedding endpoint exactly once, short timeout
    assert len(svcs.calls) == 1 and svcs.calls[0][0] == "/v1/embeddings"
    assert svcs.calls[0][1]["designation"] == "embedding"
    assert svcs.calls[0][1]["input"] == ["needle"]
    # notes results labeled separately
    assert "(source: notes)" in out
    assert "MEMORY.md:3: - [2026-10-11 10:00] needle in notes" in out
    # k clamp: default 5, max 20
    out = asyncio.run(t_memory_search(ctx, {"q": "needle"}))
    assert len([l for l in out.splitlines() if l.startswith("  [")]) == 5
    out = asyncio.run(t_memory_search(ctx, {"q": "needle", "k": 100}))
    assert len([l for l in out.splitlines() if l.startswith("  [")]) == 20
    out = asyncio.run(t_memory_search(ctx, {"q": "needle", "k": "junk"}))
    assert len([l for l in out.splitlines() if l.startswith("  [")]) == 5
    # the k arg is model-visible in the tool schema
    from app.tools import TOOL_SCHEMAS
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "memory.search")
    assert "k" in schema["parameters"]["properties"], schema
    assert "conclusions" in schema["description"], schema["description"]
    print("PASS search format + k clamp (default 5, max 20)")


def test_memory_search_degraded_never_errors():
    """Embedding call fails -> notes-grep only + the one-line note."""
    ws = tempfile.mkdtemp(prefix="r76h")
    write_rows(ws, [mk("hidden conclusion", emb=[1.0, 0.0])])
    with open(os.path.join(ws, "MEMORY.md"), "w") as f:
        f.write("# Memory\n\n- [2026-10-11 10:00] needle in notes\n")
    ctx, _ = _stub_ctx(ws, raise_=True)
    out = asyncio.run(t_memory_search(ctx, {"q": "needle"}))
    assert "(vector memory unavailable)" in out, out
    assert "hidden conclusion" not in out
    assert "(source: notes)" in out
    assert "MEMORY.md:3: - [2026-10-11 10:00] needle in notes" in out
    # no conclusions file at all -> still never errors
    ws2 = tempfile.mkdtemp(prefix="r76i")
    ctx2, _ = _stub_ctx(ws2, raise_=True)
    out2 = asyncio.run(t_memory_search(ctx2, {"q": "zz"}))
    assert "no memories match" in out2 or "(source: notes)" in out2
    print("PASS degraded search: notes-only + '(vector memory unavailable)'")


# ---------------- 4. dreamer ----------------

def test_dreamer_cadence_table():
    at_last = 10
    rows = [
        ("first run, 19 conclusions", {"last_dream": None,
                                       "conclusions_at_last": 0}, 19, False),
        ("first run, 20 conclusions", {"last_dream": None,
                                       "conclusions_at_last": 0}, 20, True),
        ("2x growth, 5h since last",
         {"last_dream": (NOW - timedelta(hours=5)).isoformat(),
          "conclusions_at_last": at_last}, at_last + 2 * at_last, False),
        ("2x growth, 7h since last",
         {"last_dream": (NOW - timedelta(hours=7)).isoformat(),
          "conclusions_at_last": at_last}, at_last + 2 * at_last, True),
        ("0.5x growth, 7h since last",
         {"last_dream": (NOW - timedelta(hours=7)).isoformat(),
          "conclusions_at_last": at_last}, at_last + 5, False),
        ("any growth, 15d since last",
         {"last_dream": (NOW - timedelta(days=15)).isoformat(),
          "conclusions_at_last": at_last}, at_last + 5, True),
    ]
    print("  scenario                        due")
    print("  " + "-" * 36)
    ok = True
    for name, state, total, expect in rows:
        got = dreamer.dreamer_due(state, total, NOW)
        print(f"  {name:<32}  {str(got):<5} (expected {expect})")
        ok = ok and (got == expect)
        assert got == expect, (name, got, expect)
    # exact boundaries
    st = {"last_dream": (NOW - timedelta(seconds=dreamer.DREAMER_MIN_S))
          .isoformat(), "conclusions_at_last": 10}
    assert dreamer.dreamer_due(st, 30, NOW)          # exactly 6h + 2x -> yes
    assert not dreamer.dreamer_due(st, 29, NOW)      # 1.9x -> no
    st2 = {"last_dream": (NOW - timedelta(seconds=dreamer.DREAMER_MAX_S))
           .isoformat(), "conclusions_at_last": 10}
    assert dreamer.dreamer_due(st2, 10, NOW)         # exactly 14d -> yes
    assert ok
    print("PASS dreamer cadence table (DREAMER_FACTOR=2, MIN 6h, MAX 14d)")


def test_dreamer_apply_atomic():
    """One dream applies: dedupe -> replacement appended + source
    superseded; contradiction -> newest kept; pattern -> new dreamer
    conclusion; state updated; every original line survives."""
    ws = tempfile.mkdtemp(prefix="r76j")
    write_rows(ws, [
        mk("old fact", era="L0:1", ts="2026-10-01T00:00:00+00:00"),
        mk("mid fact", era="L0:2", ts="2026-10-05T00:00:00+00:00"),
        mk("new fact", era="L0:3", ts="2026-10-09T00:00:00+00:00"),
    ])
    before = conclusions.load_conclusions(ws)

    async def gen(prompt):
        assert "cnc_" in prompt, "the dream feed must carry conclusion ids"
        return ("```json\n"
                + json.dumps({
                    "dedupes": [{"ids": [before[0]["id"]],
                                 "text": "replacement conclusion"}],
                    "contradictions": [{"ids": [before[2]["id"],
                                                before[1]["id"]]}],
                    "patterns": ["peers prefer async updates"]})
                + "\n```")

    r = asyncio.run(dreamer.run_dream(ws, gen, ok_embed(), now=NOW))
    assert r["new"] == 2, r          # replacement + one pattern
    assert r["superseded"] == 2, r   # dedupe source + contradicted older
    rows = conclusions.load_conclusions(ws)
    assert len(rows) == 5, rows      # 3 originals + 2 new — nothing deleted
    by_id = {x["id"]: x for x in rows}
    assert by_id[before[0]["id"]]["superseded_by"] is not None
    repl = by_id[by_id[before[0]["id"]]["superseded_by"]]
    assert repl["text"] == "replacement conclusion"
    assert repl["kind"] == "dreamer"
    assert repl["premises"] == [before[0]["id"]]
    assert repl["embedding"] == [1.0, 0.0]
    # contradiction: newest kept, older superseded by it
    assert by_id[before[1]["id"]]["superseded_by"] == before[2]["id"]
    assert by_id[before[2]["id"]]["superseded_by"] is None
    pattern = [x for x in rows if x["text"] == "peers prefer async updates"]
    assert len(pattern) == 1 and pattern[0]["kind"] == "dreamer"
    # state advanced
    st = conclusions.read_dreamer_state(ws)
    assert st["last_dream"] == NOW.isoformat(), st
    assert st["conclusions_at_last"] == 5, st
    assert not os.path.exists(os.path.join(ws, "memory",
                                           "conclusions.jsonl.tmp"))
    print("PASS dreamer apply: supersede+append, contradictions, state")


def test_dreamer_failure_leaves_state_untouched():
    ws = tempfile.mkdtemp(prefix="r76k")
    write_rows(ws, [mk("fact one"), mk("fact two")])
    conclusions.write_dreamer_state(
        ws, {"last_dream": "2026-09-01T00:00:00+00:00",
             "conclusions_at_last": 1})

    async def bad_gen(prompt):
        raise RuntimeError("provider down")

    # run_dream raises -> caller-visible failure
    try:
        asyncio.run(dreamer.run_dream(ws, bad_gen, ok_embed(), now=NOW))
        raise AssertionError("run_dream must raise on generation failure")
    except RuntimeError:
        pass
    st = conclusions.read_dreamer_state(ws)
    assert st["last_dream"] == "2026-09-01T00:00:00+00:00", st
    assert st["conclusions_at_last"] == 1, st
    assert len(conclusions.load_conclusions(ws)) == 2, "no partial writes"

    # maybe_dream swallows it (never breaks the compaction hook) — the
    # state IS due (>= 14d since last dream) so the failure is exercised
    class StubAgent:
        workspace = ws
    r = asyncio.run(dreamer.maybe_dream(StubAgent(), gen_fn=bad_gen,
                                        embed_fn=ok_embed(), now=NOW))
    assert r == {}, r
    st = conclusions.read_dreamer_state(ws)
    assert st["last_dream"] == "2026-09-01T00:00:00+00:00", "state untouched"
    print("PASS dreamer failure: state untouched, maybe_dream never raises")


def test_dreamer_not_due_short_circuits():
    ws = tempfile.mkdtemp(prefix="r76l")
    write_rows(ws, [mk("x")])
    conclusions.write_dreamer_state(
        ws, {"last_dream": (NOW - timedelta(hours=1)).isoformat(),
             "conclusions_at_last": 1})
    called = []

    async def gen(prompt):
        called.append(1)
        return "{}"

    class StubAgent:
        workspace = ws
    r = asyncio.run(dreamer.maybe_dream(StubAgent(), gen_fn=gen,
                                        embed_fn=ok_embed(), now=NOW))
    assert r == {} and not called, "no generation when the trigger is quiet"
    print("PASS dreamer idle: trigger check is cheap, no generation")


if __name__ == "__main__":
    test_prompt_two_outputs_parseable()
    test_old_format_backcompat()
    test_conclusions_appended_with_embeddings()
    test_conclusions_degraded_without_embeddings()
    test_compaction_never_fails_from_conclusions()
    test_search_ranks_by_cosine()
    test_memory_search_format_and_k_clamp()
    test_memory_search_degraded_never_errors()
    test_dreamer_cadence_table()
    test_dreamer_apply_atomic()
    test_dreamer_failure_leaves_state_untouched()
    test_dreamer_not_due_short_circuits()
    print("ALL R76 TESTS PASS")
