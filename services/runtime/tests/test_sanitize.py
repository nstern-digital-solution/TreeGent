"""Issue #10: _sanitize must only collapse CONSECUTIVE duplicate user
messages (re-delivery storms), not dedupe identical content globally —
repeated short real messages ('ok', '?') were silently dropped."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app.hostclient import _sanitize  # noqa: E402


def test_repeated_user_message_separated_kept():
    # two identical real DMs with an assistant reply between them:
    # BOTH must survive (the old global content dedupe dropped the 2nd)
    msgs = [
        {"role": "system", "content": "soul"},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "great"},
        {"role": "user", "content": "ok"},
    ]
    out = _sanitize(msgs)
    users = [m["content"] for m in out if m.get("role") == "user"]
    assert users == ["ok", "ok"], f"both repeats must be kept, got {users}"
    print("PASS: identical user messages separated by a reply are both kept")


def test_consecutive_duplicates_collapse():
    # re-append / re-delivery storm: identical user lines back-to-back
    # collapse to one
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "hello"},   # real repeat later — kept
    ]
    out = _sanitize(msgs)
    users = [m["content"] for m in out if m.get("role") == "user"]
    assert users == ["hello", "hello"], f"storm collapses, later repeat kept, got {users}"
    print("PASS: consecutive duplicates collapse to one, later repeat kept")


def test_generation_failed_stubs_still_dropped():
    msgs = [
        {"role": "user", "content": "[generation failed: boom]"},
        {"role": "user", "content": "real question"},
        {"role": "user", "content": "[generation failed: boom]"},
        {"role": "assistant", "content": "answer"},
    ]
    out = _sanitize(msgs)
    users = [m["content"] for m in out if m.get("role") == "user"]
    assert users == ["real question"], f"stubs must stay dropped, got {users}"
    print("PASS: '[generation failed' stubs still dropped")


def test_repeated_notification_lines_kept():
    # identical notification lines from separate turns are not a storm
    note = "You have 1 new message(s) from felix — use chat.check to read them."
    msgs = [
        {"role": "user", "content": note},
        {"role": "assistant", "content": "checked."},
        {"role": "user", "content": note},
    ]
    out = _sanitize(msgs)
    users = [m["content"] for m in out if m.get("role") == "user"]
    assert users == [note, note], f"per-turn notifications must be kept, got {users}"
    print("PASS: repeated notification lines across turns are kept")


def test_tail_cap_2500_boundary():
    """R78: the tail-cap is 2500 — 2500 messages pass through intact,
    2501 caps to 2500 keeping the NEWEST (system prompt kept), and the
    2000-msg compaction backstop fires BEFORE the cap (trigger < cap:
    information must never silently leave the context first)."""
    from app import qtree

    def gen(n):
        return [{"role": "system", "content": "soul"}] + \
               [{"role": "user", "content": f"m{i}"} for i in range(n)]

    out = _sanitize(gen(2499))
    assert len(out) == 2500, f"2500 total must pass through, got {len(out)}"
    out = _sanitize(gen(2500))
    assert len(out) == 2500, f"2501 total must cap at 2500, got {len(out)}"
    assert out[0]["role"] == "system", "system prompt must survive the cap"
    assert out[-1]["content"] == "m2499", "newest must survive the cap"
    assert out[1]["content"] == "m1", f"oldest dropped first, got {out[1]}"
    # trigger < cap invariant (R70 preserved): the backstop compacts at
    # 2000 non-system messages, well before the 2500 cap could bite
    assert qtree.COMPACT_TRIGGER_MSGS == 2000
    assert qtree.COMPACT_TRIGGER_MSGS < 2500
    out = _sanitize(gen(2000))
    assert len(out) == 2001, \
        "at the trigger point nothing may be tail-capped away"
    print("PASS sanitize cap 2500: 2500 pass, 2501->2500 newest kept, "
          "backstop 2000 < cap")
