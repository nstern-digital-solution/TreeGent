"""Issue #26 defect 3 (services/runtime/app/tools.py): the mail.read error
message `("ERROR: ..." + ", ".join(...) if mine else "none")` parsed as
`("ERROR: ..." + ", ".join(...)) if mine else "none"` — with no personal
mailbox the whole message collapsed to just 'none'.

runtime/app/tools.py is loaded under a synthetic package name so it can
coexist with the mail service's own `app` package in one pytest session."""
import asyncio
import importlib.util
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")


def _load_runtime_tools():
    if "rtapp.tools" in sys.modules:
        return sys.modules["rtapp.tools"]
    rt = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "..", "runtime")
    pkg = types.ModuleType("rtapp")
    pkg.__path__ = [os.path.join(rt, "app")]
    sys.modules["rtapp"] = pkg
    spec = importlib.util.spec_from_file_location(
        "rtapp.tools", os.path.join(rt, "app", "tools.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rtapp.tools"] = mod
    spec.loader.exec_module(mod)
    return mod


class StubSvcs:
    def __init__(self, boxes):
        self.boxes = boxes

    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        return self.boxes


class Ctx:
    def __init__(self, boxes):
        self.agent_id = "agt_me"
        self.svcs = StubSvcs(boxes)


def test_mail_read_error_not_collapsed_to_none():
    """No personal box, unknown mailbox= target: the error must name the
    mailbox and say 'none' — not BE 'none'."""
    tools = _load_runtime_tools()
    boxes = [{"id": "mbx_s", "address": "team@example.com", "kind": "shared",
              "owner": None, "members": ["agt_other"]}]
    out = asyncio.run(tools.t_mail_read(Ctx(boxes),
                                        {"mailbox": "nope@example.com"}))
    assert out.startswith("ERROR: no readable mailbox"), \
        f"error message collapsed: {out!r}"
    assert "none" in out


def test_mail_read_error_lists_owned_mailboxes():
    """With a personal box the same message must name it (the pre-fix
    expression only worked here by accident of the precedence bug)."""
    tools = _load_runtime_tools()
    boxes = [{"id": "mbx_m", "address": "me@example.com", "kind": "personal",
              "owner": "agt_me", "members": []}]
    out = asyncio.run(tools.t_mail_read(Ctx(boxes),
                                        {"mailbox": "nope@example.com"}))
    assert out.startswith("ERROR: no readable mailbox")
    assert "me@example.com" in out
