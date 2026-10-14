"""Audit issue #64 (H4): browser SSRF — a permitted page could redirect (or a
clicked link could navigate) into loopback/private space because only the
INITIAL url was validated and Chromium follows redirects internally.

Pins: (1) the per-navigation pure helper `_nav_url_allowed` against the
redirect-target matrix (loopback, cloud metadata, RFC1918, internal
hostnames, DNS-rebinding mixed answers, allowed https, non-http schemes);
(2) the framenavigated wiring on a mock page (blocked -> bounded error in
browser state + page blanked; allowed -> untouched; subframes ignored;
handler never raises); (3) a real Chromium redirect test when the
environment has Playwright + Chromium (local server redirecting to a
loopback URL -> blocked, body never leaks into the tool result)."""
import http.server
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
os.environ.setdefault("TG_DEV", "1")

from app import tools  # noqa: E402
from app.tools import (_browser_state, _install_nav_guard,  # noqa: E402
                       _nav_url_allowed)

_REAL_GAI = socket.getaddrinfo

# hermetic resolver: named hosts answer deterministically, everything else
# (IP literals, localhost) resolves for real — no network needed anywhere
_FAKE_DNS = {
    "example.com": ["93.184.216.34"],
    "public.example": ["93.184.216.34"],
    "internal.example": ["10.0.0.5"],
    "metadata.example": ["169.254.169.254"],
    "rebind.example": ["93.184.216.34", "127.0.0.1"],   # mixed -> deny
}


@pytest.fixture
def fake_dns(monkeypatch):
    monkeypatch.delenv("TG_WEB_FETCH_ALLOW_PRIVATE", raising=False)

    def gai(host, port, *a, **kw):
        if host in _FAKE_DNS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (ip, port if isinstance(port, int) else 0))
                    for ip in _FAKE_DNS[host]]
        return _REAL_GAI(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    return gai


# ---------------- (1) pure-helper URL matrix ----------------

def test_nav_url_matrix(fake_dns):
    allowed = [
        "https://example.com/page",
        "https://public.example/next",        # redirect back to allowed https
        "http://example.com:8080/x",
    ]
    denied = [
        # redirect targets the audit named:
        "http://127.0.0.1:8010/admin",        # the host daemon's own API
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://10.0.0.5:8080/",              # RFC1918
        "http://192.168.1.1/",                # RFC1918
        "http://172.16.0.9/",                 # RFC1918
        "http://[::1]/",                      # loopback v6
        "http://localhost:8010/",             # internal hostname (/etc/hosts)
        "http://internal.example/x",          # internal hostname -> 10.0.0.5
        "http://metadata.example/x",          # hostname aliasing 169.254.169.254
        "http://rebind.example/",             # mixed public+loopback -> deny
        "ftp://example.com/x",                # non-http(s)
        "file:///etc/passwd",                 # local file read
        "data:text/html,<b>x</b>",
        "not a url",
        "",
    ]
    for u in allowed:
        ok, reason = _nav_url_allowed(u)
        assert ok, f"{u} must pass the per-navigation check, got: {reason}"
    for u in denied:
        ok, _reason = _nav_url_allowed(u)
        assert not ok, f"{u} must be rejected by the per-navigation check"
    ok, _ = _nav_url_allowed("about:blank")
    assert ok, "about:blank is the neutral browser state (no fetch)"


def test_nav_url_allowed_flag_escape_hatch(fake_dns, monkeypatch):
    # the dev escape hatch must keep working for the rehearsal lab
    monkeypatch.setenv("TG_WEB_FETCH_ALLOW_PRIVATE", "1")
    ok, _ = _nav_url_allowed("http://127.0.0.1:8010/")
    assert ok, "escape hatch must allow private targets on a dev box"


# ---------------- (2) mock-page framenavigated wiring ----------------

class FakeFrame:
    def __init__(self, url, parent=None):
        self.url = url
        self.parent_frame = parent


class FakePage:
    def __init__(self):
        self.main_frame = FakeFrame("about:blank")
        self.handlers = {}
        self.goto_calls = []

    @property
    def url(self):
        return self.main_frame.url

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kw):
        self.goto_calls.append(url)
        self.main_frame.url = url
        self.emit(self.main_frame)

    def emit(self, frame):
        for h in self.handlers.get("framenavigated", []):
            h(frame)

    def title(self):
        return "t"

    def locator(self, sel):
        return self

    def inner_text(self, timeout=None):
        return "body"


def _guarded_page():
    page, state = FakePage(), {}
    _install_nav_guard(page, state)
    assert "framenavigated" in page.handlers, "hook must be registered"
    return page, state


def test_mock_hook_blocks_disallowed_navigation(fake_dns):
    page, state = _guarded_page()
    page.emit(FakeFrame("http://127.0.0.1:8010/secret"))   # main (no parent)
    assert state.get("nav_blocked") == ("navigation blocked: "
                                        "http://127.0.0.1:8010/secret "
                                        "not allowed")
    assert page.goto_calls == ["about:blank"], "page must be blanked"


def test_mock_hook_allows_good_navigation(fake_dns):
    page, state = _guarded_page()
    page.emit(FakeFrame("https://example.com/page"))
    assert state == {}, "allowed navigation must not touch the state"
    assert page.goto_calls == [], "allowed navigation must not blank the page"


def test_mock_hook_allowed_http_retires_old_error(fake_dns):
    page, state = _guarded_page()
    state["nav_blocked"] = "navigation blocked: http://10.0.0.5/ old not allowed"
    page.emit(FakeFrame("https://example.com/now"))
    assert "nav_blocked" not in state, "a real page must retire the error"


def test_mock_hook_ignores_subframes(fake_dns):
    page, state = _guarded_page()
    page.emit(FakeFrame("http://10.0.0.5/iframe", parent=page.main_frame))
    assert state == {}, "main frame only — subframes are not re-validated"
    assert page.goto_calls == []


def test_mock_hook_never_raises(fake_dns):
    page, state = _guarded_page()

    class BrokenFrame:
        @property
        def url(self):
            raise RuntimeError("detached")

        @property
        def parent_frame(self):
            raise RuntimeError("detached")

    page.emit(BrokenFrame())            # must not propagate into Playwright
    page.emit(FakeFrame("http://10.0.0.5/x"))   # fail-closed -> blocked
    assert state.get("nav_blocked"), "ambiguous frame must fail closed"


def test_blocked_error_is_bounded(fake_dns):
    page, state = _guarded_page()
    long_url = "http://10.0.0.5/" + "a" * 5000
    page.emit(FakeFrame(long_url))
    msg = state["nav_blocked"]
    assert len(msg) <= 200 + len("navigation blocked:  not allowed"), \
        f"error must stay bounded, got {len(msg)} chars"


def test_browser_state_surfaces_error_and_blanks_page(fake_dns):
    page, state = FakePage(), {
        "nav_blocked": "navigation blocked: http://127.0.0.1:8010/x not allowed"}
    out = _browser_state(page, state)
    assert out.startswith("ERROR: navigation blocked: http://127.0.0.1:8010/x")
    assert page.goto_calls == ["about:blank"], \
        "disallowed page must be blanked before any state dump"
    out2 = _browser_state(FakePage(), {})   # unblocked path unchanged
    assert out2.startswith("url: ") and "title: t\n\nbody" in out2


# ---------------- (3) real Chromium redirect tests ----------------

def _start_server():
    """Local server: /ok returns a marker, /redirect 302s to a LOOPBACK url
    serving a secret marker that must never reach a tool result."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location",
                                 f"http://127.0.0.1:{srv.server_address[1]}"
                                 f"/secret")
                self.end_headers()
            elif self.path.startswith("/redirect-loop"):
                self.send_response(302)
                self.send_header("Location", self.path.split("=", 1)[-1])
                self.end_headers()
            elif self.path == "/secret":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"TOP-SECRET-INTERNAL")
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK-PAGE")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _need_browser(monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.delenv("TG_WEB_FETCH_ALLOW_PRIVATE", raising=False)
    # Chromium resolves *.localhost to loopback itself (probe-verified), so
    # the allowed entry URL reaches the local server while the Python-side
    # check sees a public answer for it
    def gai(host, port, *a, **kw):
        if host == "site.localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34",
                      port if isinstance(port, int) else 0))]
        return _REAL_GAI(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", gai)


def _run_action(ws, action, args):
    out = tools._sync_browser_action(ws, action, args)
    if "playwright is not installed" in out or "Chromium is not installed" in out:
        pytest.skip("browser unavailable on this host")
    return out


def test_real_redirect_to_loopback_blocked(tmp_path, monkeypatch):
    _need_browser(monkeypatch)
    srv = _start_server()
    ws = str(tmp_path / "ws")
    try:
        out = _run_action(ws, "open",
                          {"url": f"http://site.localhost:"
                                  f"{srv.server_address[1]}/redirect"})
        assert "TOP-SECRET-INTERNAL" not in out, \
            f"redirect target content leaked: {out!r}"
        assert out.startswith("ERROR: navigation blocked:"), out
        assert f"http://127.0.0.1:{srv.server_address[1]}/secret" in out
    finally:
        _run_action(ws, "close", {})
        srv.shutdown()


def test_real_post_goto_check_catches_hook_miss(tmp_path, monkeypatch):
    # belt+suspenders: with the hook sabotaged, the post-goto final-url
    # check alone must still blank the redirect target and report it
    _need_browser(monkeypatch)
    monkeypatch.setattr(tools, "_install_nav_guard", lambda page, state: None)
    srv = _start_server()
    ws = str(tmp_path / "ws")
    try:
        out = _run_action(ws, "open",
                          {"url": f"http://site.localhost:"
                                  f"{srv.server_address[1]}/redirect"})
        assert "TOP-SECRET-INTERNAL" not in out, \
            f"post-goto check missed the redirect: {out!r}"
        assert out.startswith("ERROR: navigation blocked:"), out
    finally:
        _run_action(ws, "close", {})
        srv.shutdown()


def test_real_allowed_page_still_works(tmp_path, monkeypatch):
    _need_browser(monkeypatch)
    srv = _start_server()
    ws = str(tmp_path / "ws")
    try:
        out = _run_action(ws, "open",
                          {"url": f"http://site.localhost:"
                                  f"{srv.server_address[1]}/ok"})
        assert "OK-PAGE" in out, out
        assert not out.startswith("ERROR:"), out
    finally:
        _run_action(ws, "close", {})
        srv.shutdown()
