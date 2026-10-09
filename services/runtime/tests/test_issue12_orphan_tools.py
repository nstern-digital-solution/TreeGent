"""Issue #12 regression: every context cut must land on a message-group
boundary. A kept 'tool' result whose parent assistant(tool_calls) was
trimmed has no surviving tool_call_id parent and is rejected with 400 by
strict OpenAI-compatible providers every turn — and the corrupt start was
re-saved each turn (self-perpetuating death loop)."""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import compact as C  # noqa: E402
from app.hostclient import _sanitize  # noqa: E402


def _tool_exchange(tc_id="c1"):
    """assistant(tool_calls) -> tool result, the indivisible group."""
    return [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": tc_id, "type": "function",
                         "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": tc_id,
         "content": "result for " + tc_id},
    ]


def test_sanitize_tailcap_no_leading_tool():
    """Count tail-cap landing mid tool-exchange (the issue's repro:
    max_messages=4) must snap forward to a safe boundary."""
    msgs = [{"role": "system", "content": "soul"},
            {"role": "user", "content": "q1"}]
    msgs += _tool_exchange("c1")
    msgs += [{"role": "assistant", "content": "done"},
             {"role": "user", "content": "q2"}]
    out = _sanitize(msgs, max_messages=4)
    roles = [m["role"] for m in out]
    assert roles[0] == "system", roles
    assert "tool" not in roles, f"orphan tool result survived the cap: {roles}"
    assert roles[1] != "tool"
    print("PASS issue #12 tail-cap: roles =", roles)


def test_sanitize_budget_trim_no_leading_tool():
    """The 2M-char budget trim (tail = tail[1:]) landing mid tool-exchange
    must not leave a leading orphan tool result either."""
    msgs = [{"role": "system", "content": "soul"},
            {"role": "user", "content": "q"}]
    msgs += [{"role": "assistant", "content": "p" * 310_000,
              "tool_calls": [{"id": "c9", "type": "function",
                              "function": {"name": "run",
                                           "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c9",
              "content": "t" * 100_000},
             {"role": "user", "content": "u" * 800_000},
             {"role": "assistant", "content": "a" * 800_000}]
    out = _sanitize(msgs, max_messages=500)
    roles = [m["role"] for m in out]
    # invariant (per review on #17): no orphan tool result may survive any
    # cut — a tool msg is healthy iff a kept assistant carries its tool_call
    assert not [m for m in out if m.get("role")=="tool"
                and not any(a.get("tool_calls") and any(c["id"]==m["tool_call_id"] for c in a["tool_calls"]) for a in out)], "orphan tool survived"
    # the trim must stay minimal: the two newest big messages survive
    assert any((m.get("content") or "").startswith("u") for m in out)
    assert any((m.get("content") or "").startswith("a") for m in out)
    print("PASS issue #12 budget trim: roles =", roles)


def test_sanitize_cut_at_group_start_keeps_group():
    """A cut landing exactly at the assistant(tool_calls) keeps the whole
    group — the guard must not over-trim."""
    msgs = [{"role": "system", "content": "soul"},
            {"role": "user", "content": "q0"},
            {"role": "user", "content": "q1"}]
    msgs += _tool_exchange("c2")
    msgs += [{"role": "user", "content": "q2"}]
    out = _sanitize(msgs, max_messages=4)   # tail = [assistant(tc), tool, q2]
    roles = [m["role"] for m in out]
    assert roles == ["system", "assistant", "tool", "user"], roles
    print("PASS issue #12 group start: roles =", roles)


def _stub_agent(msgs):
    class StubAgent:
        def __init__(self, msgs):
            self.messages = msgs
            self.workspace = tempfile.mkdtemp(prefix="qtree_i12")
            self.regened = False

        def _regen_system_prompt(self):
            self.regened = True
    return StubAgent(msgs)


async def _ok_gen(*_a):
    return "stub summary"


def test_compact_drop_no_orphan_tool():
    """maybe_compact's drop set landing mid tool-exchange must extend past
    the orphan tool results so kept messages start at a safe boundary."""
    C._gen = _ok_gen
    msgs = [{"role": "system", "content": "s"}]
    for i in range(299):                    # fillers before the exchange
        msgs.append({"role": "user", "content": f"filler {i}"})
    msgs += _tool_exchange("cc")            # boundary lands between the two
    for i in range(199):                    # 500 non-system msgs total
        msgs.append({"role": "user", "content": f"tail filler {i}"})
    a = _stub_agent(msgs)
    asyncio.run(C.maybe_compact(a))
    kept = [m for m in a.messages if m.get("role") != "system"]
    roles = [m["role"] for m in kept]
    assert roles[0] != "tool", f"kept transcript starts at orphan tool: {roles}"
    assert not any(m.get("tool_call_id") == "cc" for m in a.messages), \
        "orphan tool result (parent assistant dropped) survived compaction"
    # the guard drops ONLY the orphan tool(s), nothing more
    assert len(kept) == 199, f"expected 199 kept, got {len(kept)}"
    print("PASS issue #12 compaction: kept starts at", roles[0],
          "count", len(kept))


def test_compact_drop_at_group_start_keeps_group():
    """Drop boundary landing at the assistant(tool_calls) keeps the whole
    group — the guard must not over-trim."""
    C._gen = _ok_gen
    msgs = [{"role": "system", "content": "s"}]
    for i in range(300):                    # drop set = exactly the fillers
        msgs.append({"role": "user", "content": f"filler {i}"})
    msgs += _tool_exchange("cd")            # kept from the group start
    for i in range(198):
        msgs.append({"role": "user", "content": f"tail filler {i}"})
    a = _stub_agent(msgs)
    asyncio.run(C.maybe_compact(a))
    kept = [m for m in a.messages if m.get("role") != "system"]
    roles = [m["role"] for m in kept]
    assert roles[:2] == ["assistant", "tool"], roles[:2]
    assert any(m.get("tool_call_id") == "cd" for m in kept), \
        "complete tool group must survive when the cut lands at its start"
    print("PASS issue #12 compaction group start: roles[:2] =", roles[:2])
