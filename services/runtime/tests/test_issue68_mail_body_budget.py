"""Issue #68: mail.read bodies were silently truncated at 500 chars — no
marker, no continuation, no argument. An agent reading a long mail saw a
fragment and could not know anything was missing (and after #67, mail.read
is the ONLY path to mail content).

Contract pinned here: budgeted by default, HONEST when truncated, and
re-readable in full on explicit request.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

import services.runtime.app.tools as T  # noqa: E402

# random bodies: prefix/suffix windows are unique, so "in"/"not in"
# assertions cannot overlap trivially (a cyclic body made them ambiguous)
import random as _random
_rng = _random.Random(68)
LONG = "".join(_rng.choice("abcdefghijklmnopqrstuvwxyz")
               for _ in range(4312))
MID = "".join(_rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
              for _ in range(1200))
SHORT = "short body"


class StubServices:
    def __init__(self, msgs):
        self.msgs = msgs

    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        if path == "/mailboxes":
            return [{"id": "mbx_p", "address": "me@example.com",
                     "kind": "personal", "owner": "agt_me", "members": []}]
        if path.endswith("/messages"):
            return self.msgs
        raise AssertionError(f"unexpected call {path}")


def ctx(svcs, ws):
    return T.ToolContext("agt_me", ws, svcs)


def run(coro):
    return asyncio.run(coro)


def _msg(text):
    return {"id": "mail_1", "ts": "2026-10-14 12:00", "direction": "in",
            "from": "bob@example.com", "subject": "quarterly",
            "text": text, "attachments": []}


def test_default_budget_marks_truncation_with_size_and_handle():
    out = run(T.t_mail_read(ctx(StubServices([_msg(LONG)]),
                                 tempfile.mkdtemp()), {}))
    assert "…[truncated: 500 of 4312 chars — re-read with body=full]" in out
    assert LONG[:500] in out and LONG[500:600] not in out


def test_short_body_has_no_marker():
    out = run(T.t_mail_read(ctx(StubServices([_msg(SHORT)]),
                                 tempfile.mkdtemp()), {}))
    assert SHORT in out
    assert "truncated" not in out


def test_body_full_reads_everything():
    out = run(T.t_mail_read(ctx(StubServices([_msg(LONG)]),
                                 tempfile.mkdtemp()), {"body": "full"}))
    assert LONG in out                      # whole body, no marker
    assert "truncated" not in out


def test_body_int_budget_and_progression_handle():
    out = run(T.t_mail_read(ctx(StubServices([_msg(MID)]),
                                 tempfile.mkdtemp()), {"body": 800}))
    assert "…[truncated: 800 of 1200 chars — re-read with body=full]" in out
    # at the hard cap the handle points at the exact remaining ceiling
    out2 = run(T.t_mail_read(ctx(StubServices([_msg(LONG)]),
                                  tempfile.mkdtemp()),
                             {"body": 20000}))
    assert LONG in out2                     # 4312 < 20000: nothing clipped
    assert "truncated" not in out2


def test_bad_body_value_falls_back_to_default():
    out = run(T.t_mail_read(ctx(StubServices([_msg(MID)]),
                                 tempfile.mkdtemp()), {"body": "banana"}))
    assert "…[truncated: 500 of 1200 chars — re-read with body=full]" in out
