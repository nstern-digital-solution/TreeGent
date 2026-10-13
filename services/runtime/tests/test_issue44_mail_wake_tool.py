"""Issue #44 regression: hosted mail-reason wake injections pointed at
mail.check — which lists APPROVALS only. Mail bodies are read via mail.read
(R59, later than the R56-era injection text), so every mail wake burned 1-3
tool calls rediscovering that live.

Prompt-shape contract:
- a mail-reason wake injection NEVER says "mail.check" and DOES say "mail.read";
- approval-reason injections still say "mail.check" (that is the right tool
  for them — a blanket rewrite would break those);
- the system-prompt discovery line lists BOTH tools.
"""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402


class FakeHC:
    """Minimal hosted client: canned pending() payload (issue #7 pattern)."""

    def __init__(self, inbox, wakes):
        self._inbox = inbox
        self._wakes = wakes

    async def pending(self, agent_id):
        return {"inbox": self._inbox, "wakes": self._wakes}


def make_agent(wakes=(), inbox=()):
    # skip __init__ (fs + identity setup) — the methods under test only
    # need the pending-list attributes
    a = loop.Agent.__new__(loop.Agent)
    a.id = "agt_test"
    a.hc = FakeHC(list(inbox), list(wakes))
    a._pending_inbox_ids = []
    a._pending_wake_ids = []
    a._pending_senders = []
    a._reply_ctx = None
    return a


def collect(agent):
    return asyncio.run(agent._collect_injections_hosted())


def test_mail_wake_points_at_mail_read():
    agent = make_agent(wakes=[{"wake_id": "wke_pull_x_agt_test",
                               "reason": "mail",
                               "detail": "new mail from a@example.com"}])
    lines = collect(agent)
    assert len(lines) == 1
    assert "mail.read" in lines[0], f"mail wake must point at mail.read: {lines[0]}"
    assert "mail.check" not in lines[0], \
        f"mail wake must never point at approvals-only mail.check: {lines[0]}"
    # the wake is still ack-scoped exactly as before the fix
    assert agent._pending_wake_ids == ["wke_pull_x_agt_test"]
    print("PASS mail wake points at mail.read, never mail.check")


def test_approval_wake_still_points_at_mail_check():
    agent = make_agent(wakes=[{"wake_id": "wke_apr_1", "reason": "approval",
                               "approval_id": "apr_1"}])
    lines = collect(agent)
    assert len(lines) == 1
    assert "mail.check" in lines[0], \
        f"approval wake must still point at mail.check: {lines[0]}"
    assert agent._pending_wake_ids == ["wke_apr_1"]
    print("PASS approval wake still points at mail.check")


def test_mixed_wakes_rewritten_per_reason():
    # one poll carrying both reasons: each line points at ITS tool
    agent = make_agent(wakes=[
        {"wake_id": "wke_pull_x_agt_test", "reason": "mail",
         "detail": "new mail from a@example.com"},
        {"wake_id": "wke_apr_2", "reason": "approval", "approval_id": "apr_2"},
    ])
    lines = collect(agent)
    assert len(lines) == 2
    mail_line = next(l for l in lines if "mail waiting" in l)
    apr_line = next(l for l in lines if "approval" in l)
    assert "mail.read" in mail_line and "mail.check" not in mail_line
    assert "mail.check" in apr_line
    print("PASS mixed wakes: each reason points at its own tool")


def test_system_prompt_discovery_lists_both_mail_tools():
    a = loop.Agent.__new__(loop.Agent)
    a.actor = {"_id": "agt_test", "display_name": "Test Agent"}
    a.name = "Test"
    prompt = a._base_prompt()
    discovery = [l for l in prompt.splitlines()
                 if "memory.search" in l or "ws.read). No other" in l
                 or "discover yourself with tools" in l]
    assert discovery, "discovery line missing from the system prompt"
    block = " ".join(discovery)
    assert "mail.check" in block and "mail.read" in block, \
        f"discovery line must list both mail.check and mail.read: {block}"
    print("PASS system prompt discovery line lists mail.check + mail.read")
