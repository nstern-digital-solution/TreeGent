"""Issue #60 runtime side — attachment visibility + mail.save_attachment.

The ruling (2026-10-13), verbatim:
  "We need to add this feature but we should not store files by default
  this is dangerous. Instead we should let the agent know there is a file /
  files attached. And give it a tool where it can decide to download
  attachments and specify storage location."

Contracts pinned here (the MAIL service side lives in
services/mail/tests/test_issue60_attachments.py):
- mail.read renders '| attach: report.pdf (13 KB, application/pdf)' per
  attachment (notify-only — nothing downloads);
- mail.save_attachment {mail, filename, to, overwrite} resolves the
  attachment from the mail's stored metadata, calls the download endpoint
  and writes via safe_ws_path: default attachments/<filename>, traversal
  REJECTED, no overwrite without overwrite=true, ambiguous filename errors
  list the candidates, unknown mail/attachment give clear errors;
- mail wakes render '(N attachments)' so the agent knows to look;
- the system prompt discovery line lists the tool next to mail.read.

Stubbed Services client returning canned bytes — no network, no Mongo.
"""
# pyright: reportAttributeAccessIssue=false
# (deliberate stub wiring below: fake service/db doubles, no Mongo)
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import loop  # noqa: E402
from app import tools as T  # noqa: E402

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

ATT_REPORT = {"id": "att_1", "filename": "report.pdf",
              "content_type": "application/pdf", "size": 13312}
ATT_NOTES = {"id": "att_2", "filename": "notes.txt",
             "content_type": "text/plain", "size": 2048}


class StubServices:
    """Mail-service double: canned mailboxes/messages + canned attachment
    bytes. Records every call."""

    def __init__(self, boxes=None, messages=None, blob=b"PDFBYTES"):
        self.calls = []
        self.boxes = boxes if boxes is not None else [
            {"id": "mbx_p", "address": "me@example.com", "kind": "personal",
             "owner": "agt_me", "members": []}]
        self.messages = messages if messages is not None else {}
        self.blob = blob

    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        self.calls.append(("call", path, dict(params or {})))
        if path == "/mailboxes":
            return self.boxes
        if path.endswith("/messages"):
            mbx = path.split("/")[2]
            return self.messages.get(mbx, [])
        raise AssertionError(f"unexpected service call: {path}")

    async def _call_bytes(self, base, path, params=None, timeout=120.0):
        self.calls.append(("bytes", path, dict(params or {})))
        return self.blob


def ctx(svcs, workspace):
    return T.ToolContext("agt_me", workspace, svcs)


def run(coro):
    return asyncio.run(coro)


def msg_with(atts, mid="mail_1", mbx="mbx_p"):
    return {"id": mid, "mailbox_id": mbx, "direction": "in",
            "from": "sender@example.com", "to": "me@example.com",
            "subject": "report", "text": "see attached", "ts": str(NOW),
            "attachments": list(atts)}


# ---------------- mail.read attach lines ----------------

def test_mail_read_renders_attach_lines():
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT, ATT_NOTES])]})
    out = run(T.t_mail_read(ctx(svcs, tempfile.mkdtemp()), {}))
    line = next(l for l in out.splitlines() if "report" in l and "←" in l)
    assert "| attach: report.pdf (13 KB, application/pdf)" in line, line
    assert "| attach: notes.txt (2 KB, text/plain)" in line, line
    # review #61: the row id IS the documented input of mail.save_attachment
    # — mail.read must surface it (pins the handoff contract, not just render)
    assert "(id: mail_1)" in line, line


def test_mail_read_to_save_attachment_round_trip():
    """The production handoff: derive the id from mail.read's OUTPUT and
    feed it to mail.save_attachment (review #61 blocking finding)."""
    import os
    ws = tempfile.mkdtemp()
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT])]})
    out = run(T.t_mail_read(ctx(svcs, ws), {}))
    import re as _re
    mid = _re.search(r"\(id: (mail_[a-z0-9_]+)\)", out).group(1)
    assert mid == "mail_1", mid
    # bytes come back via the stubbed download endpoint path
    svcs2 = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT])]})
    out2 = run(T.t_mail_save_attachment(ctx(svcs2, ws),
                                        {"mail": mid, "filename": "report.pdf"}))
    assert "ERROR" not in out2, out2
    saved = os.path.join(ws, "attachments", "report.pdf")
    assert os.path.exists(saved), out2
    with open(saved, "rb") as fh:
        assert fh.read() == b"PDFBYTES"


def test_mail_read_no_attachments_no_attach_segment():
    svcs = StubServices(messages={"mbx_p": [msg_with([])]})
    out = run(T.t_mail_read(ctx(svcs, tempfile.mkdtemp()), {}))
    assert "attach:" not in out


# ---------------- mail.save_attachment: happy paths ----------------

def test_save_attachment_round_trip_default_path():
    ws = tempfile.mkdtemp()
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT, ATT_NOTES])]},
                        blob=b"PDFBYTES")
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "report.pdf"}))
    assert out == "saved attachments/report.pdf (8 bytes)", out
    with open(os.path.join(ws, "attachments", "report.pdf"), "rb") as f:
        assert f.read() == b"PDFBYTES"
    dl = [c for c in svcs.calls if c[0] == "bytes"]
    assert len(dl) == 1
    assert dl[0][1] == "/mailboxes/mbx_p/messages/mail_1/attachments/att_1"


def test_save_attachment_custom_to():
    ws = tempfile.mkdtemp()
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_NOTES])]},
                        blob=b"xy")
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "notes.txt",
                        "to": "inbox/out.txt"}))
    assert "saved inbox/out.txt (2 bytes)" == out
    with open(os.path.join(ws, "inbox", "out.txt"), "rb") as f:
        assert f.read() == b"xy"


def test_save_attachment_resolvable_by_id_when_names_collide():
    """The ambiguity error lists attachment ids — passing one resolves."""
    ws = tempfile.mkdtemp()
    dup1 = {"id": "att_a", "filename": "dup.txt", "content_type": "text/plain",
            "size": 1}
    dup2 = {"id": "att_b", "filename": "dup.txt", "content_type": "text/plain",
            "size": 2}
    svcs = StubServices(messages={"mbx_p": [msg_with([dup1, dup2])]}, blob=b"b")
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "att_b"}))
    assert out == "saved attachments/dup.txt (1 bytes)", out
    assert [c for c in svcs.calls if c[0] == "bytes"][0][1].endswith(
        "/attachments/att_b")


# ---------------- mail.save_attachment: refusals ----------------

def test_save_attachment_traversal_rejected():
    ws = tempfile.mkdtemp()
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT])]})
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "report.pdf",
                        "to": "../evil.pdf"}))
    assert out.startswith("ERROR") and "escapes workspace" in out, out
    assert not os.path.exists(os.path.join(os.path.dirname(ws), "evil.pdf"))
    assert not [c for c in svcs.calls if c[0] == "bytes"], \
        "a refused path must never trigger a download"


def test_save_attachment_refuses_overwrite_without_flag():
    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, "attachments"))
    with open(os.path.join(ws, "attachments", "report.pdf"), "w") as f:
        f.write("old")
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT])]})
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "report.pdf"}))
    assert out.startswith("ERROR") and "overwrite" in out
    with open(os.path.join(ws, "attachments", "report.pdf")) as f:
        assert f.read() == "old"          # untouched
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "report.pdf",
                        "overwrite": True}))
    assert not out.startswith("ERROR")
    with open(os.path.join(ws, "attachments", "report.pdf"), "rb") as f:
        assert f.read() == b"PDFBYTES"


def test_save_attachment_ambiguous_filename_lists_candidates():
    ws = tempfile.mkdtemp()
    dup1 = {"id": "att_a", "filename": "dup.txt", "content_type": "text/plain",
            "size": 1024}
    dup2 = {"id": "att_b", "filename": "dup.txt", "content_type": "text/html",
            "size": 2048}
    svcs = StubServices(messages={"mbx_p": [msg_with([dup1, dup2])]})
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "dup.txt"}))
    assert out.startswith("ERROR") and "matches 2 attachments" in out, out
    assert "att_a" in out and "att_b" in out, \
        f"ambiguity error must list the candidates: {out}"
    assert "1 KB" in out and "2 KB" in out
    assert not [c for c in svcs.calls if c[0] == "bytes"]


def test_save_attachment_unknown_mail_and_attachment():
    ws = tempfile.mkdtemp()
    svcs = StubServices(messages={"mbx_p": [msg_with([ATT_REPORT])]})
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_ghost", "filename": "report.pdf"}))
    assert out.startswith("ERROR") and "no readable mail message" in out
    out = run(T.t_mail_save_attachment(
        ctx(svcs, ws), {"mail": "mail_1", "filename": "ghost.pdf"}))
    assert out.startswith("ERROR") and "available" in out
    assert "report.pdf" in out, "error lists what IS attached"
    out = run(T.t_mail_save_attachment(ctx(svcs, ws), {"mail": "mail_1"}))
    assert out.startswith("ERROR") and "filename" in out


# ---------------- registration + prompt ----------------

def test_tool_and_schema_registered():
    assert "mail.save_attachment" in T.TOOLS
    names = [s["name"] for s in T.TOOL_SCHEMAS]
    assert "mail.save_attachment" in names
    schema = next(s for s in T.TOOL_SCHEMAS
                  if s["name"] == "mail.save_attachment")
    props = schema["parameters"]["properties"]
    assert set(props) >= {"mail", "filename", "to", "overwrite"}


def test_system_prompt_lists_tool_next_to_mail_read():
    a = loop.Agent.__new__(loop.Agent)
    a.actor = {"_id": "agt_test", "display_name": "Test Agent"}
    a.name = "Test"
    prompt = a._base_prompt()
    assert "mail.save_attachment" in prompt, \
        "system prompt must discover the new tool"
    # discovery line(s): next to mail.read
    for line in prompt.splitlines():
        if "mail.read" in line and "discover" in line or (
                "mail.save_attachment" in line and "mail.read" in line):
            assert "mail.read" in line
            break
    else:
        raise AssertionError("mail.save_attachment not next to mail.read")
    # #44 contract intact
    assert "mail.check" in prompt


# ---------------- wake rendering: '(N attachments)' ----------------

class FakeHC:
    def __init__(self, inbox, wakes):
        self._inbox, self._wakes = inbox, wakes

    async def pending(self, agent_id):
        return {"inbox": self._inbox, "wakes": self._wakes}


def hosted_agent(wakes):
    a = loop.Agent.__new__(loop.Agent)
    a.id = "agt_me"
    a.hc = FakeHC([], list(wakes))
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_senders, a._reply_ctx = [], None
    return a


def test_hosted_mail_wake_shows_attachment_count():
    """The mail service writes '(N attachments)' into the wake detail; the
    rendered wake line shows it so the agent knows to look."""
    a = hosted_agent([{"wake_id": "wke_pull_x", "reason": "mail",
                       "detail": "new mail from s@x (2 attachments)"}])
    lines = asyncio.run(a._collect_injections_hosted())
    assert len(lines) == 1
    assert "(2 attachments)" in lines[0], lines[0]
    assert "mail.read" in lines[0]
    a2 = hosted_agent([{"wake_id": "wke_pull_y", "reason": "mail",
                        "detail": "new mail from s@x"}])
    lines2 = asyncio.run(a2._collect_injections_hosted())
    assert "attachments" not in lines2[0], lines2[0]


class StubCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class StubColl:
    def __init__(self, rows=()):
        self.rows = list(rows)

    @staticmethod
    def _match(row, q):
        for k, v in (q or {}).items():
            if isinstance(v, dict) and "$in" in v:
                if row.get(k) not in v["$in"]:
                    return False
            elif row.get(k) != v:
                return False
        return True

    def find(self, q, *a, **k):
        return StubCursor([r for r in self.rows if self._match(r, q)])

    async def find_one(self, q, *a, **k):
        for r in self.rows:
            if self._match(r, q):
                return dict(r)
        return None


class StubDB:
    def __init__(self, wakes, mails):
        self.wake_events = StubColl(wakes)
        self.mail_messages = StubColl(mails)


class EmptySvcs:
    async def _call(self, base, path, method="GET", body=None, params=None,
                    timeout=60.0):
        return {"messages": []}


def test_central_mail_wake_shows_attachment_count(monkeypatch):
    """Central tier renders the mail line from the mail doc — the same
    '(N attachments)' suffix so both tiers notify identically."""
    db = StubDB(
        wakes=[{"_id": "wke_1", "agent_id": "agt_me", "reason": "mail",
                "consumed": False, "created_at": NOW,
                "mail_message_id": "mail_1"}],
        mails=[{"_id": "mail_1", "from_addr": "s@x", "subject": "hi",
                "text": "body", "attachments": [ATT_REPORT, ATT_NOTES]}])
    monkeypatch.setattr(loop, "_cdb", lambda: db)
    a = loop.Agent.__new__(loop.Agent)
    a.id, a.name = "agt_me", "Me"
    a.hc = None
    a.svcs = EmptySvcs()
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    a._pending_senders, a._reply_ctx = [], None
    a._pending_central = False
    lines = asyncio.run(a.collect_injections())
    assert len(lines) == 1
    assert lines[0].endswith("(2 attachments)"), lines[0]
    # no attachments -> no suffix
    db.mail_messages.rows[0]["attachments"] = []
    a._pending_inbox_ids, a._pending_wake_ids = [], []
    lines = asyncio.run(a.collect_injections())
    assert "attachments" not in lines[0], lines[0]
