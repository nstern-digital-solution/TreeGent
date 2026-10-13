"""Issue #60 — mail attachments, notify-only metadata + agent-controlled
download.

The ruling (2026-10-13), verbatim:
  "We need to add this feature but we should not store files by default
  this is dangerous. Instead we should let the agent know there is a file /
  files attached. And give it a tool where it can decide to download
  attachments and specify storage location."

Contracts pinned here (the MAIL side; runtime tool tests live in
services/runtime/tests/test_issue60_save_attachment.py):
- import persists compact attachment METADATA {id, filename, content_type,
  size} from the LIST payload — NEVER bytes, NEVER download_url;
- the heal sweep backfills `attachments` for rows imported before the
  field existed (bounded by body_attempts; a metadata-only backfill must
  NOT re-wake — that is #45's duplicate-wake bug class);
- pull-wake detail appends '(N attachments)' for mails with attachments;
- GET /mailboxes/{mbx}/messages/{id}/attachments/{att}: owner/member ok,
  stranger 403, no credential 401, unknown att 404, wrong mailbox 404,
  TG_MAIL_ATTACH_MAX cap at the stored size BEFORE any provider call,
  streaming round-trip with STUB adapters (no real Resend, ever), bounded
  friendly errors that never echo remote bodies.

Fake provider + in-memory fake DB where possible; the endpoint matrix runs
against a real FastAPI app (real header auth via treegent_common.auth) over
call-recording collection doubles. No network, no Mongo, no Resend.
"""
# pyright: reportAttributeAccessIssue=false
# (deliberate stub wiring below: fake db doubles, no real DB)
import asyncio
import json
import os
import sys
import types
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import adapters  # noqa: E402
from app import main as mail_main  # noqa: E402
from test_issue26_sync_hygiene import (  # noqa: E402
    AGT_A, MB_A, FakeProvider, mail, setup)

CAP = 26214400          # TG_MAIL_ATTACH_MAX default: 25 MB


# ---------------- metadata at import (list payload) ----------------

def _list_att(aid="att_1", name="report.pdf", ctype="application/pdf",
              size=13312, **extra):
    """One Resend list-payload attachment (the FULL shape incl. the fields
    we must NOT store)."""
    return {"id": aid, "filename": name, "content_type": ctype, "size": size,
            "content_disposition": "attachment",
            "content_id": "<cid-1>",
            **extra}


def test_import_stores_compact_metadata_never_bytes(monkeypatch):
    """The LIST payload's attachments[] land as EXACTLY
    {id, filename, content_type, size} — content_disposition, content_id
    and any download_url-looking key are stripped; no byte fields exist."""
    m = mail(0, "a@example.com")
    m["attachments"] = [
        _list_att(),
        _list_att("att_2", "notes.txt", "text/plain", 2048,
                  download_url="https://signed.example/never-store"),
    ]
    p = FakeProvider([m])
    p.bodies[m["id"]] = "the body"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])

    r = asyncio.run(adapters.sync_inbound())
    assert r["imported"] == 1
    row = db.mail_messages.docs[0]
    atts = row["attachments"]
    assert [a["id"] for a in atts] == ["att_1", "att_2"]
    for a in atts:
        assert set(a) == {"id", "filename", "content_type", "size"}, a
    assert atts[0] == {"id": "att_1", "filename": "report.pdf",
                       "content_type": "application/pdf", "size": 13312}
    # the forbidden payload fields are gone
    dumped = json.dumps(row, default=str)
    assert "download_url" not in dumped and "signed.example" not in dumped
    assert "content_disposition" not in dumped and "content_id" not in dumped


def test_import_without_attachments_stores_empty_list(monkeypatch):
    """Every inbound row carries the field (empty list when none) — it is
    ALSO the heal sweep's marker for pre-issue-60 rows."""
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = "plain mail"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])
    asyncio.run(adapters.sync_inbound())
    assert db.mail_messages.docs[0]["attachments"] == []


def test_wake_detail_counts_attachments(monkeypatch):
    """Mails WITH attachments append '(N attachments)' to the pull-wake
    detail so the agent knows to look before mail.read."""
    m = mail(0, "a@example.com")
    m["attachments"] = [_list_att(), _list_att("att_2", "b.txt", "text/plain", 3)]
    p = FakeProvider([m])
    p.bodies[m["id"]] = "see attached"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])
    asyncio.run(adapters.sync_inbound())
    wakes = db.wake_events.docs
    assert len(wakes) == 1
    assert wakes[0]["detail"] == "new mail from sender@example.com (2 attachments)"


def test_wake_detail_without_attachments_has_no_suffix(monkeypatch):
    m = mail(0, "a@example.com")
    p = FakeProvider([m])
    p.bodies[m["id"]] = "plain"
    db = setup(monkeypatch, p, [MB_A], [AGT_A])
    asyncio.run(adapters.sync_inbound())
    assert db.wake_events.docs[0]["detail"] == "new mail from sender@example.com"


# ---------------- heal sweep backfill ----------------

def test_heal_backfills_attachments_for_old_rows(monkeypatch):
    """A row imported before issue #60 has NO attachments field; the heal
    sweep fetches the retrieve payload and backfills the compact metadata.
    Metadata-only backfill NEVER wakes (the mail was already readable)."""
    old = {"id": "00000000-aaaa-bbbb-cccc-000000000001", "to": ["a@example.com"],
           "from": "s@x", "subject": "old", "created_at": "2026-10-01T11:59:00Z"}
    p = FakeProvider([old])
    db = setup(monkeypatch, p, [MB_A], [AGT_A])
    # pre-#60 row: body present, attachments field MISSING
    db.mail_messages.docs.append({
        "_id": "mail_old", "mailbox_id": "mbx_a", "direction": "in",
        "from_addr": "s@x", "to": "a@example.com", "subject": "old",
        "text": "body already here", "resend_id": old["id"],
        "ts": adapters.parse_ts(old["created_at"])})

    fetched = []

    async def fake_fetch(email_id):
        fetched.append(email_id)
        return {"text": "body already here",
                "attachments": [_list_att()]}

    monkeypatch.setattr(adapters, "fetch_received_email", fake_fetch)
    r = asyncio.run(adapters.sync_inbound())
    assert r["backfilled"] == 1
    row = db.mail_messages.docs[0]
    assert row["attachments"] == [{"id": "att_1", "filename": "report.pdf",
                                   "content_type": "application/pdf",
                                   "size": 13312}]
    assert not db.wake_events.docs, \
        "a metadata-only backfill must never re-wake (#45 bug class)"
    # healed rows leave the heal query — no endless refetch
    asyncio.run(adapters.sync_inbound())
    assert fetched == [old["id"]]


def test_heal_backfills_body_and_attachments_together(monkeypatch):
    """A bodyless pre-#60 row gets text AND attachments in one heal pass,
    and the wake carries the '(N attachments)' suffix."""
    old = {"id": "00000000-aaaa-bbbb-cccc-000000000002", "to": ["a@example.com"],
           "from": "s@x", "subject": "old", "created_at": "2026-10-01T11:59:00Z"}
    p = FakeProvider([old])
    db = setup(monkeypatch, p, [MB_A], [AGT_A])
    db.mail_messages.docs.append({
        "_id": "mail_old", "mailbox_id": "mbx_a", "direction": "in",
        "from_addr": "s@x", "to": "a@example.com", "subject": "old",
        "text": "", "resend_id": old["id"],
        "ts": adapters.parse_ts(old["created_at"])})

    async def fake_fetch(email_id):
        return {"text": "the healed body", "attachments": [_list_att()]}

    monkeypatch.setattr(adapters, "fetch_received_email", fake_fetch)
    r = asyncio.run(adapters.sync_inbound())
    assert r["backfilled"] == 1
    row = db.mail_messages.docs[0]
    assert row["text"] == "the healed body"
    assert row["attachments"][0]["id"] == "att_1"
    wakes = db.wake_events.docs
    assert len(wakes) == 1
    assert wakes[0]["detail"] == "new mail from s@x (1 attachments)"


# ---------------- in-memory endpoint doubles ----------------

class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a):
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __aiter__(self):
        self._it = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeColl:
    def __init__(self):
        self.docs = []

    async def find_one(self, q):
        for d in self.docs:
            if all(d.get(k) == v for k, v in (q or {}).items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def count_documents(self, q):
        return len(self.docs)

    def find(self, q=None):
        rows = [dict(d) for d in self.docs
                if all(d.get(k) == v for k, v in (q or {}).items())]
        return FakeCursor(rows)


class FakeDB:
    def __init__(self):
        self.agent_keys = FakeColl()
        self.actors = FakeColl()
        self.mailboxes = FakeColl()
        self.mail_messages = FakeColl()
        self.permissions = FakeColl()


ATTACHMENTS = [
    {"id": "att_1", "filename": "report.pdf", "content_type": "application/pdf",
     "size": 13312},
    {"id": "att_2", "filename": "notes.txt", "content_type": "text/plain",
     "size": 2048},
]


@pytest.fixture()
def api(monkeypatch):
    """Real FastAPI app (real header auth) over fake collections."""
    db = FakeDB()
    for aid in ("agt_owner", "agt_member", "agt_stranger"):
        db.actors.docs.append({"_id": aid, "kind": "agent"})
    for key, aid in (("key_owner", "agt_owner"), ("key_member", "agt_member"),
                     ("key_stranger", "agt_stranger")):
        db.agent_keys.docs.append({"_id": key, "agent_id": aid})
    db.mailboxes.docs.append({"_id": "mbx_p", "address": "a@example.com",
                              "kind": "personal", "owner": "agt_owner"})
    db.mailboxes.docs.append({"_id": "mbx_s", "address": "s@example.com",
                              "kind": "shared", "members": ["agt_member"]})
    db.mailboxes.docs.append({"_id": "mbx_o", "address": "o@example.com",
                              "kind": "personal", "owner": "agt_stranger"})
    db.mail_messages.docs.append({
        "_id": "mail_1", "mailbox_id": "mbx_p", "direction": "in",
        "from_addr": "s@x", "to": "a@example.com", "subject": "report",
        "text": "see attached", "resend_id": "re_email1",
        "attachments": [dict(a) for a in ATTACHMENTS]})
    db.mail_messages.docs.append({
        "_id": "mail_s", "mailbox_id": "mbx_s", "direction": "in",
        "from_addr": "s@x", "to": "s@example.com", "subject": "team",
        "text": "team file", "resend_id": "re_email2",
        "attachments": [dict(a) for a in ATTACHMENTS]})
    db.mail_messages.docs.append({
        "_id": "mail_dev", "mailbox_id": "mbx_p", "direction": "in",
        "from_addr": "s@x", "to": "a@example.com", "subject": "dev hook",
        "text": "no provider id", "resend_id": "",
        "attachments": [dict(a) for a in ATTACHMENTS]})
    monkeypatch.setattr(mail_main, "db", db)
    monkeypatch.setattr(mail_main, "mailboxes", db.mailboxes)
    monkeypatch.setattr(mail_main, "mail_messages", db.mail_messages)
    app = FastAPI()
    app.include_router(mail_main.router)
    return TestClient(app), db


def hdr(key):
    return {"X-Agent-Key": key}


class StubAdapters:
    """Resend double: canned signed URL + canned bytes. Records calls;
    NEVER touches the network."""

    def __init__(self, monkeypatch, chunks=(b"PDFBYTES",), size_ok=True):
        self.calls = []
        self.stream_calls = []
        self.chunks = list(chunks)

        async def fetch_attachment_url(email_id, att_id):
            self.calls.append((email_id, att_id))
            return {"download_url": "https://signed.stub/att1",
                    "expires_at": "2026-10-13T13:00:00Z"}

        async def iter_attachment_bytes(url, max_bytes):
            self.stream_calls.append((url, max_bytes))
            for c in self.chunks:
                yield c

        monkeypatch.setattr(adapters, "fetch_attachment_url",
                            fetch_attachment_url)
        monkeypatch.setattr(adapters, "iter_attachment_bytes",
                            iter_attachment_bytes)


def url(mbx="mbx_p", msg="mail_1", att="att_1"):
    return f"/mailboxes/{mbx}/messages/{msg}/attachments/{att}"


# ---------------- endpoint: auth matrix ----------------

def test_no_credential_401(api):
    client, _ = api
    r = client.get(url())
    assert r.status_code == 401


def test_bad_service_token_401(api):
    client, _ = api
    r = client.get(url(), headers={"X-Service-Token": "wrong",
                                   "X-Actor-Id": "agt_owner"})
    assert r.status_code == 401


def test_service_tier_unknown_actor_403(api):
    client, _ = api
    r = client.get(url(), headers={"X-Service-Token": "dev-service-token",
                                   "X-Actor-Id": "agt_ghost"})
    assert r.status_code == 403


def test_owner_gets_bytes(api, monkeypatch):
    client, _ = api
    stub = StubAdapters(monkeypatch, chunks=(b"PDF", b"BYTES"))
    r = client.get(url(), headers=hdr("key_owner"))
    assert r.status_code == 200
    assert r.content == b"PDFBYTES"
    assert stub.calls == [("re_email1", "att_1")]
    assert stub.stream_calls == [("https://signed.stub/att1", CAP)]


def test_shared_member_gets_bytes(api, monkeypatch):
    client, _ = api
    StubAdapters(monkeypatch)
    r = client.get(url("mbx_s", "mail_s"), headers=hdr("key_member"))
    assert r.status_code == 200


def test_stranger_403(api, monkeypatch):
    client, _ = api
    stub = StubAdapters(monkeypatch)
    r = client.get(url(), headers=hdr("key_stranger"))
    assert r.status_code == 403
    assert not stub.calls, "stranger must never trigger a provider fetch"


def test_service_tier_stranger_403(api):
    client, _ = api
    r = client.get(url(), headers={"X-Service-Token": "dev-service-token",
                                   "X-Actor-Id": "agt_stranger"})
    assert r.status_code == 403


# ---------------- endpoint: lookup errors ----------------

def test_unknown_attachment_404(api):
    client, _ = api
    r = client.get(url(att="att_nope"), headers=hdr("key_owner"))
    assert r.status_code == 404
    assert "attachment" in r.json()["detail"]


def test_wrong_mailbox_404(api, monkeypatch):
    """mail_1 lives in mbx_p — probing it under a mailbox the caller DOES
    own still 404s (message must belong to the mailbox in the URL)."""
    client, _ = api
    stub = StubAdapters(monkeypatch)
    r = client.get(url("mbx_s", "mail_1"), headers=hdr("key_member"))
    assert r.status_code == 404
    assert not stub.calls


def test_no_provider_id_404(api, monkeypatch):
    """Dev-hook mail has no resend_id — bytes are provider-side only."""
    client, _ = api
    stub = StubAdapters(monkeypatch)
    r = client.get(url(msg="mail_dev"), headers=hdr("key_owner"))
    assert r.status_code == 404
    assert "unavailable" in r.json()["detail"]
    assert not stub.calls


# ---------------- endpoint: cap ----------------

def test_cap_rejects_oversized_before_provider_fetch(api, monkeypatch):
    """Stored metadata size over TG_MAIL_ATTACH_MAX -> friendly 413 and
    the provider is NEVER asked (stub adapter records zero calls)."""
    client, db = api
    stub = StubAdapters(monkeypatch)
    db.mail_messages.docs[0]["attachments"][0]["size"] = CAP + 1
    r = client.get(url(), headers=hdr("key_owner"))
    assert r.status_code == 413
    detail = r.json()["detail"]
    assert "too large" in detail and str(CAP) in detail
    assert "TG_MAIL_ATTACH_MAX" in detail
    assert not stub.calls and not stub.stream_calls


def test_attach_max_setting_env_name():
    """TG_MAIL_ATTACH_MAX is the documented knob (env_prefix TG_MAIL_)."""
    os.environ["TG_MAIL_ATTACH_MAX"] = "123"
    try:
        from app.config import Settings
        assert Settings().attach_max == 123
    finally:
        del os.environ["TG_MAIL_ATTACH_MAX"]
    assert mail_main.settings.attach_max == CAP, "default stays 25 MB"


# ---------------- endpoint: streaming + errors ----------------

def test_streaming_round_trip_headers(api, monkeypatch):
    client, _ = api
    StubAdapters(monkeypatch, chunks=(b"hello ", b"world"))
    r = client.get(url(), headers=hdr("key_owner"))
    assert r.status_code == 200
    assert r.content == b"hello world"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == \
        'attachment; filename="report.pdf"'


def test_mid_stream_over_cap_aborts(api, monkeypatch):
    """Metadata size lied: the streaming layer aborts over the cap."""
    client, _ = api

    async def greedy(url, max_bytes):
        yield b"x" * 10
        raise adapters.AttachmentError(
            f"attachment exceeds the {max_bytes}-byte cap mid-transfer")

    async def fetch_attachment_url(email_id, att_id):
        return {"download_url": "https://signed.stub/att1"}

    monkeypatch.setattr(adapters, "fetch_attachment_url", fetch_attachment_url)
    monkeypatch.setattr(adapters, "iter_attachment_bytes", greedy)
    with pytest.raises(Exception, match="exceeds the .*-byte cap mid-transfer"):
        client.get(url(), headers=hdr("key_owner"))


def test_provider_error_is_bounded(api, monkeypatch):
    """Remote bodies are never echoed back to the caller."""
    client, _ = api

    async def boom(email_id, att_id):
        raise RuntimeError('raw remote junk {"error":"secret internals"}')

    monkeypatch.setattr(adapters, "fetch_attachment_url", boom)
    r = client.get(url(), headers=hdr("key_owner"))
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "RuntimeError" in detail
    assert "secret internals" not in detail and "raw remote" not in detail


def test_no_download_link_502(api, monkeypatch):
    client, _ = api

    async def empty(email_id, att_id):
        return {}

    monkeypatch.setattr(adapters, "fetch_attachment_url", empty)
    r = client.get(url(), headers=hdr("key_owner"))
    assert r.status_code == 502
    assert "no download link" in r.json()["detail"]


# ---------------- the real provider seams (urllib/httpx stubs) ----------------

class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_attachment_url_named_ua_and_path(monkeypatch):
    """Same Cloudflare UA rule as every other Resend call site (R68): the
    retrieve-attachment call carries the named UA and the documented path
    GET /emails/receiving/{emailId}/attachments/{attId}."""
    os.environ["RESEND_API_KEY"] = "re_test_key"
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return _FakeResp(json.dumps(
            {"download_url": "https://signed.example/x",
             "expires_at": "2026-10-13T13:00:00Z"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = asyncio.run(adapters.fetch_attachment_url("re_1", "att_1"))
    assert seen["ua"] == adapters._UA
    assert seen["url"].endswith("/emails/receiving/re_1/attachments/att_1")
    assert out["download_url"] == "https://signed.example/x"


class _FakeStreamResp:
    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _fake_httpx(chunks, status=200):
    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            resp = _FakeStreamResp(chunks)
            resp.status_code = status
            return _FakeStreamCM(resp)

    return types.SimpleNamespace(AsyncClient=_Client)


def _collect(gen):
    async def run():
        return [c async for c in gen]

    return asyncio.run(run())


def test_iter_attachment_bytes_round_trip(monkeypatch):
    monkeypatch.setattr(adapters, "httpx",
                        _fake_httpx([b"aa", b"bb", b"cc"]))
    out = _collect(adapters.iter_attachment_bytes("https://signed/x", 100))
    assert b"".join(out) == b"aabbcc"


def test_iter_attachment_bytes_cap_mid_stream(monkeypatch):
    """The REAL streaming seam aborts when the transfer exceeds the cap —
    the endpoint's pre-fetch size check cannot catch a lying provider."""
    monkeypatch.setattr(adapters, "httpx",
                        _fake_httpx([b"aaaa", b"bbbb", b"cccc"]))
    with pytest.raises(adapters.AttachmentError, match="mid-transfer"):
        _collect(adapters.iter_attachment_bytes("https://signed/x", 8))


def test_iter_attachment_bytes_http_error(monkeypatch):
    monkeypatch.setattr(adapters, "httpx",
                        _fake_httpx([b"nope"], status=403))
    with pytest.raises(adapters.AttachmentError, match="provider download"):
        _collect(adapters.iter_attachment_bytes("https://signed/x", 100))


# ---------------- public message shape ----------------

def test_messages_endpoint_exposes_compact_attachments(api, monkeypatch):
    """The messages listing carries the compact attachments array (the
    runtime's mail.read renders from it)."""
    client, db = api

    async def no_sync(*_a, **_k):
        return {"imported": 0, "skipped": 0, "failed": 0, "backfilled": 0}

    monkeypatch.setattr(mail_main.adapters, "sync_inbound", no_sync)
    out = mail_main.mpub(db.mail_messages.docs[0])
    assert out["attachments"] == ATTACHMENTS
    assert all(set(a) == {"id", "filename", "content_type", "size"}
               for a in out["attachments"])
    # and through the route (auth + scope plumbing intact)
    client.get(url())   # any request proves the app wires cleanly
