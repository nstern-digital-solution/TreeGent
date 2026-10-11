"""Regression: every Resend HTTP call must carry a named User-Agent.

Cloudflare fronts api.resend.com with a browser-signature rule that 403-bans
the default Python UA ("error code: 1010") BEFORE auth — key regeneration
can never fix it, and the inbound pull silently degraded to cache on every
call since the integration shipped (the outbound send would bounce the same
way). These tests pin the UA on all three call sites by stubbing
urllib.request.urlopen and asserting the Request object's headers.
"""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import adapters  # noqa: E402


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_list_received_sends_named_ua(monkeypatch=None):
    os.environ["RESEND_API_KEY"] = "re_test_key"
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _FakeResp(b'{"count": 0, "data": []}')

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        out = adapters.list_received_emails(limit=5)
    finally:
        urllib.request.urlopen = orig
    assert seen["ua"] == adapters._UA, \
        f"list must send named UA, sent {seen['ua']!r}"
    assert out == {"count": 0, "data": []}


def test_fetch_body_sends_named_ua():
    import asyncio
    os.environ["RESEND_API_KEY"] = "re_test_key"
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        return _FakeResp(b'{"id": "x", "text": "hi"}')

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        out = asyncio.run(adapters.fetch_received_email("abc"))
    finally:
        urllib.request.urlopen = orig
    assert seen["ua"] == adapters._UA, \
        f"body fetch must send named UA, sent {seen['ua']!r}"
    assert "/emails/receiving/abc" in seen["url"]
    assert out["text"] == "hi"


def test_send_resend_sends_named_ua():
    import asyncio, json as _json
    os.environ["RESEND_API_KEY"] = "re_test_key"
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["idem"] = req.get_header("Idempotency-key")
        return _FakeResp(_json.dumps({"id": "email_1"}).encode())

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        out = asyncio.run(adapters.send_resend({
            "_id": "mail_1", "from_addr": "a@e.nstern.de",
            "to": "b@example.com", "subject": "s", "text": "t"}))
    finally:
        urllib.request.urlopen = orig
    assert seen["ua"] == adapters._UA, \
        f"send must send named UA, sent {seen['ua']!r}"
    assert seen["idem"] is not None, "issue #13 idempotency key kept"
    assert out["via"] == "resend" and out["provider_id"] == "email_1"


if __name__ == "__main__":
    test_list_received_sends_named_ua()
    test_fetch_body_sends_named_ua()
    test_send_resend_sends_named_ua()
    print("ALL UA TESTS PASS")
