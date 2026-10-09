"""Issue #11: model echoes of injected blocks must not persist in
transcripts forever — non-system content is scrubbed of <soul>/<memory>/
<episodic-memory> blocks and capped at 20k chars (head+tail) on load AND
on save; the system message is never touched by these layers."""
import json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import hostclient  # noqa: E402

ECHO = ("Here is my reply.\n<soul>\nYou are an agent.\n</soul>\n"
        "blah <memory>\nnote 1\n</memory><memory>\nnote 2\n</memory>\n"
        "<episodic-memory l=\"0\">\n- [ts] old digest\n</episodic-memory>\n"
        "tail of reply.")


def test_issue11_assistant_echo_scrubbed():
    msgs = [{"role": "system", "content": "<soul>\nKEEP ME\n</soul>"},
            {"role": "assistant", "content": ECHO}]
    out = hostclient._sanitize(msgs)
    body = out[1]["content"]
    assert "<soul>" not in body and "<memory>" not in body
    assert "<episodic-memory" not in body
    assert "[scrubbed echo]" in body
    assert "Here is my reply." in body and "tail of reply." in body
    # system message untouched (its own strip_soul path handles it)
    assert out[0]["content"] == "<soul>\nKEEP ME\n</soul>"
    print("PASS issue #11: assistant echo scrubbed, system untouched")


def test_issue11_giant_message_capped():
    huge = "H" * 200_000 + "MIDDLE" + "T" * 200_000
    out = hostclient._sanitize([{"role": "user", "content": "hi"},
                                {"role": "assistant", "content": huge}])
    body = out[1]["content"]
    assert len(body) < 16_000, f"capped body is {len(body)} chars"
    assert body.startswith("H" * 100)
    assert body.endswith("T" * 100)
    assert "[... truncated " in body
    assert "MIDDLE" not in body          # the cut middle is gone
    print("PASS issue #11: 400k-char assistant capped to head+tail + marker")


def test_issue11_system_never_scrubbed_or_capped():
    big_sys = "<soul>\n" + "S" * 300_000 + "\n</soul>"
    out = hostclient._sanitize([{"role": "system", "content": big_sys},
                                {"role": "user", "content": "hi"}])
    assert out[0]["content"] == big_sys
    assert "[scrubbed" not in out[0]["content"]
    assert "[... truncated" not in out[0]["content"]
    print("PASS issue #11: system message exempt from scrub/cap")


def test_issue11_existing_victim_cleaned_at_load():
    """One-time cleanup: a live transcript with a giant echo is healed
    on the next load_messages -> _sanitize pass (no external jsonl edit)."""
    ws = tempfile.mkdtemp(prefix="tg_i11")
    victim = {"role": "assistant",
              "content": "start\n" + ("<soul>\n" + "x" * 1000 + "\n</soul>"
                                      "<memory>\n" + "y" * 1000 + "\n</memory>"
                                      ) * 80 + "\nend " + "z" * 250_000}
    hostclient.session_path(ws).write_text(
        json.dumps({"role": "system", "content": "<soul>\nSYS\n</soul>"})
        + "\n" + json.dumps(victim) + "\n")
    out = hostclient.load_messages(ws)
    body = out[1]["content"]
    assert len(body) < 16_000, f"victim still {len(body)} chars after load"
    assert "<soul>" not in body and "<memory>" not in body
    assert "[scrubbed echo]" in body and "[... truncated " in body
    # and the bounded form is what gets persisted back
    hostclient.save_messages(ws, out)
    on_disk = json.loads(hostclient.session_path(ws).read_text()
                         .splitlines()[1])
    assert len(on_disk["content"]) < 16_000
    assert "<soul>" not in on_disk["content"]
    print("PASS issue #11: existing 393k-style victim scrubbed+capped at load")


def test_issue11_save_bounds_new_echoes():
    ws = tempfile.mkdtemp(prefix="tg_i11s")
    msgs = [{"role": "system", "content": "plain system"},
            {"role": "assistant", "content": "answer\n<memory>\necho\n</memory>"}]
    hostclient.save_messages(ws, msgs)
    line = hostclient.session_path(ws).read_text().splitlines()[1]
    assert "<memory>" not in line and "[scrubbed echo]" in line
    # the caller's own list is not rewritten in place
    assert "<memory>" in msgs[1]["content"]
    print("PASS issue #11: save_messages bounds fresh echoes too")
