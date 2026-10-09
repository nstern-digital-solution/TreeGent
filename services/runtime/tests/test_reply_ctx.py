"""Regression tests (issue #7): the auto-reply target is derived from the
structured sender list, never parsed back out of the notification sentence.

The old code split the rendered line on " from " / " received at" — the
hosted template has no " received at" marker, so the 'sender' swallowed the
trailing "— use chat.check to read them." clause, and a multi-sender join
was not one username at all.
"""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402


class FakeHC:
    """Minimal hosted client: canned pending() payload."""

    def __init__(self, inbox):
        self._inbox = inbox

    async def pending(self, agent_id):
        return {"inbox": self._inbox, "wakes": []}


def make_agent(inbox):
    # skip __init__ (fs + identity setup) — the method under test only
    # needs the pending-list attributes
    a = loop.Agent.__new__(loop.Agent)
    a.id = "agt_test"
    a.hc = FakeHC(inbox)
    a._pending_inbox_ids = []
    a._pending_wake_ids = []
    a._pending_senders = []
    a._reply_ctx = None
    return a


def collect(agent):
    return asyncio.run(agent._collect_injections_hosted())


def test_single_sender_reply_ctx():
    agent = make_agent([{"inbox_id": "in1", "sender_username": "felix"}])
    lines = collect(agent)
    assert lines == ["You have 1 new message(s) from felix — use "
                     "chat.check to read them."]
    assert agent._pending_senders == ["felix"]
    assert agent._reply_ctx == {"sender": "felix"}
    print("PASS single sender: reply target is exactly 'felix'")


def test_two_senders_no_reply_ctx():
    agent = make_agent([{"inbox_id": "in1", "sender_username": "felix"},
                        {"inbox_id": "in2", "sender_username": "anna"}])
    lines = collect(agent)
    assert lines == ["You have 2 new message(s) from anna, felix — use "
                     "chat.check to read them."]
    # several senders: no single auto-reply target — the names stay in
    # the notification line and the agent picks chat.send targets itself
    assert agent._reply_ctx is None
    assert agent._pending_senders == ["anna", "felix"]
    print("PASS two senders: no reply context, names stay in the line")


def test_no_garbage_clause_in_sender():
    # the exact repro of the bug: everything after " from " in the
    # sentence used to become the username, including the dash clause
    agent = make_agent([{"inbox_id": "in1", "sender_username": "felix"}])
    collect(agent)
    sender = (agent._reply_ctx or {}).get("sender")
    assert sender == "felix"
    assert "chat.check" not in sender
    assert "read them" not in sender
    assert "\u2014" not in sender
    print("PASS no garbage: the dash clause never leaks into the sender")


def test_reply_ctx_follows_latest_collect():
    agent = make_agent([{"inbox_id": "in1", "sender_username": "felix"}])
    collect(agent)
    assert agent._reply_ctx == {"sender": "felix"}
    # next poll carries two senders — the old single-sender target must
    # not survive into it
    agent.hc = FakeHC([{"inbox_id": "in2", "sender_username": "felix"},
                       {"inbox_id": "in3", "sender_username": "anna"}])
    agent._pending_inbox_ids = []
    collect(agent)
    assert agent._reply_ctx is None
    print("PASS fresh data: a multi-sender poll clears the old target")
