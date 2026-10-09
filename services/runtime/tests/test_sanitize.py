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
