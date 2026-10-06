"""Agent tools (R42). Every tool call carries the agent's OWN key; services
derive caller identity from the key server-side (R45) — an agent can never
act as anyone else. Untrusted content (mail bodies, chat messages) enters
the transcript only as clearly-delimited DATA inside tool results."""
import asyncio
import json
import os
import re
import subprocess
import threading
import time

import httpx

from .config import settings


# ---------------- service client (key-authenticated) ----------------

class Services:
    def __init__(self, agent_key: str, agent_id: str = ""):
        self.key = agent_key
        self.agent_id = agent_id  # set by supervisor (key->agent from DB)

    async def _call(self, base: str, path: str, method: str = "GET",
                    body: dict | None = None, params: dict | None = None,
                    timeout: float = 60.0) -> dict:
        # R45: the agent key IS the agent-tier credential. The shared
        # service token rides along ONLY when the runtime itself runs on
        # the trusted central box (settings.service_token is empty on
        # agent hosts — provisioning writes none), letting central-mode
        # calls reach service-tier endpoints (inbox polling). On agent
        # hosts the header is absent and R56 host-tier endpoints are used.
        headers = {"X-Agent-Key": self.key}
        if settings.service_token:
            headers["X-Service-Token"] = settings.service_token
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, f"{base}{path}", headers=headers,
                                json=body, params=params)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(f"service {r.status_code}: "
                               f"{json.dumps(data)[:300]}")
        return data


# ---------------- path guard for workspace files ----------------

def safe_ws_path(workspace: str, rel: str) -> str:
    """Resolve rel inside workspace; refuse escapes."""
    root = os.path.realpath(workspace)
    p = os.path.realpath(os.path.join(root, rel))
    if not (p == root or p.startswith(root + os.sep)):
        raise PermissionError(f"path escapes workspace: {rel!r}")
    return p


# ---------------- tool implementations ----------------

class ToolContext:
    def __init__(self, agent_id: str, workspace: str, svcs: Services,
                 host_client=None):
        self.agent_id = agent_id
        self.workspace = workspace
        self.svcs = svcs
        self.host_client = host_client   # hosted mode: host-tier client



async def t_chat_send(ctx: ToolContext, args: dict) -> str:
    """Send a chat message. to=<actor username>, body=<text>."""
    to = args.get("to") or args.get("recipient") or args.get("user") \
        or args.get("username") or args.get("send_to")
    body = args.get("body") or args.get("message") or args.get("text")
    if not to or not body:
        return (f"ERROR: need 'to' (username) and 'body' (text); got keys "
                f"{sorted(args)}")
    r = await ctx.svcs._call(settings.chat_url, "/internal/resolve-actor",
                             "GET", params={"username": to})
    target = r.get("actor_id")
    if not target:
        return f"ERROR: no actor named {to!r}"
    conv = await ctx.svcs._call(settings.chat_url,
                                f"/conversations/dm/{target}", "POST")
    await ctx.svcs._call(
        settings.chat_url,
        f"/conversations/{conv['id']}/messages",
        "POST", body={"body": body})
    return f"sent to {to}"


async def t_chat_check(ctx: ToolContext, args: dict) -> str:
    """Read inbox messages. Filters (all optional): sender=<username>,
    unread=<bool>, since=<ts>, limit=<int>. Default: newest 20, any state."""
    params = {"agent_id": ctx.agent_id}
    if args.get("sender"):
        params["sender"] = str(args["sender"])
    unread = args.get("unread")
    read = args.get("read")
    # default: both states
    params["unread"] = "true" if unread in (None, True) else "false"
    params["read"] = "true" if read in (None, True) else "false"
    if args.get("since"):
        params["since"] = str(args["since"])
    try:
        params["limit"] = str(max(1, min(int(args.get("limit", 20)), 100)))
    except (TypeError, ValueError):
        params["limit"] = "20"
    # hosted runtimes have no service token — their chat.check rides the
    # host-tier API (X-Host-Id/X-Host-Key auth)
    hc = getattr(ctx, "host_client", None)
    if hc is not None:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        from urllib.parse import quote
        r = await hc._call(
            "GET", f"/internal/host/messages/{quote(ctx.agent_id)}?{qs}")
    else:
        r = await ctx.svcs._call(settings.chat_url, "/internal/agent-messages",
                                 params=params)
    msgs = r.get("messages", [])
    if not msgs:
        return "no messages matching the filter"
    return "\n".join(
        f"[{m.get('received_at', '')}] {m.get('sender_username', '?')}: "
        f"{m.get('body', '')}" for m in msgs)


async def t_mail_send(ctx: ToolContext, args: dict) -> str:
    """Approval-gated mail. from=<own address or shared>, to, subject, text,
    mode=background|foreground (R39)."""
    from_mb = (args.get("from") or args.get("from_mailbox")
               or args.get("mail_from") or args.get("sender")
               or args.get("from_email") or "").strip()
    to = args.get("to") or args.get("recipient") or ""
    text = args.get("text") or args.get("body") or args.get("message") or ""
    if not from_mb or not to:
        return (f"ERROR: need 'from' (your mailbox), 'to', 'subject', "
                f"'text'; got keys {sorted(args)}")
    payload = {"from_mailbox": from_mb, "to": to,
               "subject": args.get("subject", ""),
               "text": text,
               "requester_id": ctx.agent_id,
               "mode": args.get("mode", "background")}
    r = await ctx.svcs._call(settings.mail_url, "/send", "POST", body=payload)
    return (f"mail queued for approval {r['approval_id']} "
            f"(approver decides; status via mail.check)")


async def t_mail_check(ctx: ToolContext, args: dict) -> str:
    """Pending approvals: waiting on me + requested by me (R37)."""
    inbox = await ctx.svcs._call(settings.mail_url, "/approvals",
                                 params={"actor_id": ctx.agent_id,
                                         "scope": "inbox"})
    mine = await ctx.svcs._call(settings.mail_url, "/approvals",
                                params={"actor_id": ctx.agent_id,
                                        "scope": "requested"})
    lines = [f"awaiting my decision: {len(inbox)}"]
    for a in inbox[:10]:
        p = a.get("payload", {})
        lines.append(f"  {a['id']}: {a['action']} {p.get('from','')} -> "
                     f"{p.get('to','')} | {p.get('subject','')}")
    lines.append(f"requested by me: {len(mine)}")
    for a in mine[:10]:
        p = a.get("payload", {})
        lines.append(f"  {a['id']}: {a['action']} {p.get('to','')} | "
                     f"{a.get('status')}{' — ' + a['reason'] if a.get('reason') else ''}")
    return "\n".join(lines)


async def t_approval_decide(ctx: ToolContext, args: dict) -> str:
    """Approve/reject an approval waiting on ME (agent superiors only)."""
    r = await ctx.svcs._call(
        settings.mail_url,
        f"/approvals/{args['id']}/decide", "POST",
        body={"decision": args["decision"],
              "reason": args.get("reason", "")},
        params={"actor_id": ctx.agent_id})
    return f"decided: {r.get('status')}"


async def t_secrets_list(ctx: ToolContext, args: dict) -> str:
    q = args.get("q", "")
    r = await ctx.svcs._call(settings.secrets_url, "/secrets",
                              params={"q": q})
    if not r:
        return "no matching secrets in your scope"
    return "\n".join(f"{s['id']}  {s['name']}  {s.get('username') or ''}"
                     for s in r[:30])


async def t_secret_read(ctx: ToolContext, args: dict) -> str:
    r = await ctx.svcs._call(settings.secrets_url,
                              f"/secrets/{args['id']}/value")
    # R43: full fidelity — the value enters the transcript literally.
    return f"{r['name']} = {r['value']}"


async def t_secret_write(ctx: ToolContext, args: dict) -> str:
    r = await ctx.svcs._call(
        settings.secrets_url, "/secrets", "POST",
        body={"name": args["name"], "value": args["value"],
              "username": args.get("username", ""),
              "url": args.get("url", ""), "notes": args.get("notes", "")})
    return f"stored {r['id']} ({r['name']})"


async def t_exec(ctx: ToolContext, args: dict) -> str:
    """Run a shell command in the agent workspace.
    foreground (default): killed after settings.exec_timeout_s (R46: 10 min).
    background=true: detached, NO timeout (R46) — output lands in
    .tg-exec/<id>.log inside the workspace, read it with ws.read.
    Runs as the configured exec user when one exists (R46 separate user)."""
    if not settings.exec_enabled:
        return ("ERROR: exec is disabled on this host (reserved "
                "machine, R48) — command execution requires a dedicated "
                "agent host")
    cmd = args.get("cmd") or args.get("command") or ""
    if not cmd:
        return "ERROR: need 'cmd'"
    if len(cmd) > 8000:
        return "ERROR: command too long"
    background = bool(args.get("background") or args.get("bg")
                      or args.get("wait") is False)
    if settings.exec_user:
        cmd = (f"sudo -n -u {settings.exec_user} -- bash -c "
               + _sq(cmd))
    if background:
        import uuid
        logdir = os.path.join(ctx.workspace, ".tg-exec")
        os.makedirs(logdir, exist_ok=True)
        log_id = uuid.uuid4().hex[:12]
        logpath = os.path.join(logdir, f"{log_id}.log")
        with open(logpath, "w") as logf:
            subprocess.Popen(cmd, shell=True, cwd=ctx.workspace,
                             stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
        return (f"background job {log_id} started (no timeout); "
                f"output -> .tg-exec/{log_id}.log")
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=ctx.workspace,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(),
                                        timeout=settings.exec_timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        return f"ERROR: timed out after {settings.exec_timeout_s}s"
    text = out.decode(errors="replace")[:8000]
    return f"[exit {proc.returncode}]\n{text or '(no output)'}"


def _sq(s: str) -> str:
    """single-quote a string for safe shell interpolation"""
    return "'" + s.replace("'", "'\\''") + "'"


async def t_ws_write(ctx: ToolContext, args: dict) -> str:
    """Write a file inside the workspace (path-guarded)."""
    p = safe_ws_path(ctx.workspace, args["path"])
    os.makedirs(os.path.dirname(p) or ctx.workspace, exist_ok=True)
    with open(p, "w") as f:
        f.write(args["content"])
    return f"wrote {args['path']} ({len(args['content'])} bytes)"


async def t_ws_read(ctx: ToolContext, args: dict) -> str:
    p = safe_ws_path(ctx.workspace, args["path"])
    if not os.path.exists(p):
        return "ERROR: no such file in workspace"
    with open(p) as f:
        return f.read()[:16000]


async def t_ws_ls(ctx: ToolContext, args: dict) -> str:
    p = safe_ws_path(ctx.workspace, args.get("path", "."))
    if not os.path.isdir(p):
        return "ERROR: not a directory"
    entries = sorted(os.listdir(p))[:200]
    return "\n".join(entries) or "(empty)"


async def t_web_fetch(ctx: ToolContext, args: dict) -> str:
    """Fetch a URL, strip to readable text (full browser automation later)."""
    url = args["url"]
    if not re.match(r"^https?://", url):
        return "ERROR: http(s) URL required"
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "TreeGentAgent/0.1"})
    if r.status_code != 200:
        return f"ERROR: HTTP {r.status_code}"
    html = r.text
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
    return text[:12000]


async def t_web_search(ctx: ToolContext, args: dict) -> str:
    if not settings.search_backend:
        return "ERROR: web search disabled on this deployment (no backend configured)"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(settings.search_backend,
                        params={"q": args["q"], "limit": 8})
    if r.status_code != 200:
        return f"ERROR: search backend {r.status_code}"
    items = r.json() if r.content else []
    return "\n".join(f"{i.get('title')}: {i.get('url')}" for i in items[:8]) \
        or "no results"


async def t_memory_search(ctx: ToolContext, args: dict) -> str:
    from . import soul as SOUL
    return SOUL.memory_search(ctx.workspace, args["q"])


async def t_memory_write(ctx: ToolContext, args: dict) -> str:
    from . import soul as SOUL
    return SOUL.memory_append(ctx.workspace, args["text"])


# ---------------- browser (Playwright: one persistent context per agent) -----
# Browser-Use semantics inside one tool: open/read/click/type/screenshot/close.
# The Chromium profile lives in <workspace>/browser-profile, so cookies and
# logins survive across turns. Playwright is a blocking sync API — every call
# runs in a worker thread (asyncio.to_thread) so the async loop never blocks,
# serialized by a lock because sync-API objects are not thread-safe.
# Missing Chromium (e.g. the central box, where agent hosts are provisioned
# separately) degrades to a clear error string — never a crashed turn.

BROWSER_IDLE_REAP_S = 900            # close contexts idle > 15 min
BROWSER_TIMEOUT_MS = 30_000          # per-action cap (~30s)
BROWSER_TEXT_CAP = 12000             # readable-text budget per read
_BROWSERS: dict[str, dict] = {}      # workspace -> {pw, ctx, page, last_used}
_BROWSER_LOCK = threading.Lock()     # sync Playwright: one call at a time


def _browser_reap() -> None:
    """Close idle contexts (Rosa review #3): N agents must not mean N
    resident Chromiums forever. Runs on every action under the lock."""
    now = time.monotonic()
    for ws in [w for w, s in _BROWSERS.items()
               if now - s.get("last_used", now) > BROWSER_IDLE_REAP_S]:
        try:
            _BROWSERS.pop(ws)["pw"].stop()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


import atexit


def _browser_shutdown() -> None:
    """Rosa review #4: daemon exit must not leak Chromium processes."""
    with _BROWSER_LOCK:
        for ws in list(_BROWSERS):
            try:
                _BROWSERS.pop(ws)["pw"].stop()
            except Exception:  # noqa: BLE001
                pass


atexit.register(_browser_shutdown)


def _browser_state(page) -> str:
    """Small state dump after an action: where we are + readable text."""
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = "(no title)"
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        text = ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()[:BROWSER_TEXT_CAP]
    return (f"url: {page.url}\ntitle: {title}\n\n{text or '(empty page)'}")


def _browser_open(workspace: str) -> dict:
    """Launch the agent's persistent Chromium context (blocking)."""
    from playwright.sync_api import sync_playwright
    profile = os.path.join(workspace, "browser-profile")
    os.makedirs(profile, exist_ok=True)
    pw = sync_playwright().start()
    try:
        bctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=True,
            viewport={"width": 1280, "height": 720},
            args=["--disable-dev-shm-usage"],
        )
    except Exception as e:  # noqa: BLE001 — missing browser/deps is normal
        pw.stop()
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            raise RuntimeError(
                "browser unavailable on this host: Chromium is not "
                "installed (run: uv run playwright install --with-deps "
                "chromium) — agent hosts get it at provisioning time")
        raise RuntimeError(f"browser failed to start: {msg[:300]}")
    bctx.set_default_timeout(BROWSER_TIMEOUT_MS)
    bctx.set_default_navigation_timeout(BROWSER_TIMEOUT_MS)
    page = bctx.pages[0] if bctx.pages else bctx.new_page()
    return {"pw": pw, "ctx": bctx, "page": page}


def _sync_browser_action(workspace: str, action: str, args: dict) -> str:
    """Blocking browser work — ONLY ever called via asyncio.to_thread."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return ("ERROR: browser tool unavailable on this host: playwright "
                "is not installed (run: uv sync / uv run playwright install "
                "--with-deps chromium)")
    with _BROWSER_LOCK:
        _browser_reap()
        state = _BROWSERS.get(workspace)
        if state is None:
            if action == "close":
                return "no browser session open"
            try:
                state = _browser_open(workspace)
            except RuntimeError as e:
                return f"ERROR: {e}"
            _BROWSERS[workspace] = state
        state["last_used"] = time.monotonic()
        page = state["page"]

        if action == "open":
            url = args.get("url") or ""
            if not re.match(r"^https?://", url):
                return "ERROR: http(s) URL required"
            page.goto(url, wait_until="load")
            return _browser_state(page)
        if action == "text":
            return _browser_state(page)
        if action == "click":
            sel = args.get("selector") or ""
            if not sel:
                return "ERROR: need 'selector'"
            page.click(sel)
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:  # noqa: BLE001 — SPA clicks never fire load
                pass
            return _browser_state(page)
        if action == "type":
            sel = args.get("selector") or ""
            if not sel:
                return "ERROR: need 'selector'"
            if args.get("text") is None:
                return "ERROR: need 'text'"
            # R(rosa)#2: fill() fires no keyboard events — controlled inputs
            # (React etc.) and keydown-driven logins silently no-op. Click the
            # field first, clear it, then type with real key events.
            try:
                page.click(sel, timeout=5000)
                page.fill(sel, "", timeout=5000)   # clear existing value
            except Exception:  # noqa: BLE001 — some fields resist click/clear
                pass
            page.type(sel, str(args["text"]))
            return _browser_state(page)
        if action == "screenshot":
            rel = args.get("path") or "screenshot.png"
            abs_p = safe_ws_path(workspace, rel)
            os.makedirs(os.path.dirname(abs_p) or workspace, exist_ok=True)
            page.screenshot(path=abs_p, full_page=True)
            return (f"saved {rel} ({os.path.getsize(abs_p)} bytes) "
                    f"in workspace")
        if action == "close":
            try:
                state["ctx"].close()
                state["pw"].stop()
            finally:
                _BROWSERS.pop(workspace, None)
            return "browser closed"
        return (f"ERROR: unknown browser action {action!r} — use open, text, "
                f"click, type, screenshot or close")


async def t_browser(ctx: ToolContext, args: dict) -> str:
    """Drive the agent's headless browser. action=open|text|click|type|
    screenshot|close. One persistent session per agent (state survives
    across turns)."""
    action = args.get("action") or ""
    if action not in ("open", "text", "click", "type", "screenshot", "close"):
        return ("ERROR: need 'action' = open | text | click | type | "
                f"screenshot | close; got {action!r}")
    try:
        # blocking Playwright work stays off the async loop; 45s hard backstop
        # above the ~30s per-action Playwright timeouts
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_browser_action, ctx.workspace,
                              action, args),
            timeout=45)
    except asyncio.TimeoutError:
        return "ERROR: browser action timed out (45s)"


TOOLS: dict = {
    "chat.send": t_chat_send,
    "chat.check": t_chat_check,
    "mail.send": t_mail_send,
    "mail.check": t_mail_check,
    "approvals.decide": t_approval_decide,
    "secrets.list": t_secrets_list,
    "secrets.read": t_secret_read,
    "secrets.write": t_secret_write,
    "exec": t_exec,
    "ws.write": t_ws_write,
    "ws.read": t_ws_read,
    "ws.ls": t_ws_ls,
    "web.fetch": t_web_fetch,
    "web.search": t_web_search,
    "browser": t_browser,
    "memory.search": t_memory_search,
    "memory.write": t_memory_write,
}

TOOL_SCHEMAS = [
    {
        "name": "chat.send", "description": "send a chat message to an actor by username",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "body": {"type": "string"},
            "required": ["to", "body"]}}},
    {
        "name": "chat.check",
        "description": "read your inbox messages. Notifications only tell you "
                       "messages exist; this returns actual content. Filters: "
                       "sender, unread, since, limit.",
        "parameters": {"type": "object", "properties": {
            "sender": {"type": "string", "description": "filter by sender username"},
            "unread": {"type": "boolean", "description": "only unread (default any)"},
            "since": {"type": "string", "description": "ISO timestamp lower bound"},
            "limit": {"type": "integer", "description": "max messages (default 20)"}}}},
    {
        "name": "mail.send", "description": "send email (ALWAYS approval-gated by your superior)",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "string", "description": "your address or a shared mailbox you are a member of"},
            "to": {"type": "string"}, "subject": {"type": "string"},
            "text": {"type": "string"},
            "mode": {"type": "string", "enum": ["background", "foreground"]},
            "required": ["from", "to", "subject", "text"]}}},
    {
        "name": "mail.check", "description": "list pending approvals: waiting on me + requested by me",
        "parameters": {"type": "object", "properties": {}}},
    {
        "name": "approvals.decide", "description": "approve or reject an approval waiting on you",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "decision": {"type": "string", "enum": ["approve", "reject"]},
            "reason": {"type": "string"}, "required": ["id", "decision"]}}},
    {
        "name": "secrets.list", "description": "search secrets in your scope (own + shared)",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}}}},
    {
        "name": "secrets.read", "description": "read one secret's value by id",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "required": ["id"]}}},
    {
        "name": "secrets.write", "description": "store a new secret",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "value": {"type": "string"},
            "username": {"type": "string"}, "url": {"type": "string"},
            "notes": {"type": "string"}, "required": ["name", "value"]}}},
    {
        "name": "exec", "description": "run a shell command in your workspace",
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string"}, "required": ["cmd"]}}},
    {
        "name": "ws.write", "description": "write a file in your workspace",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "required": ["path", "content"]}}},
    {
        "name": "ws.read", "description": "read a file from your workspace",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "required": ["path"]}}},
    {
        "name": "ws.ls", "description": "list a workspace directory",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}}},
    {
        "name": "web.fetch", "description": "fetch a URL as readable text",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "required": ["url"]}}},
    {
        "name": "web.search", "description": "web search (if enabled on this deployment)",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}, "required": ["q"]}}},
    {
        "name": "browser", "description": "drive a real headless browser in your workspace (persistent session: cookies/logins survive across turns). action=open (url=...) navigates and returns page state; text= current page as readable text; click (selector=...) clicks; type (selector=..., text=...) fills a form field; screenshot (path=...) saves a PNG into your workspace; close= end the session.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["open", "text", "click", "type",
                                "screenshot", "close"]},
            "url": {"type": "string", "description": "http(s) URL for action=open"},
            "selector": {"type": "string", "description": "CSS selector for click/type"},
            "text": {"type": "string", "description": "text to fill for action=type"},
            "path": {"type": "string", "description": "workspace path for action=screenshot (PNG)"},
            "required": ["action"]}}},
    {
        "name": "memory.search", "description": "search MEMORY.md and notes/*.md for a word or regex — your long-term memory in files",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}, "required": ["q"]}}},
    {
        "name": "memory.write", "description": "append a durable note to MEMORY.md (timestamped, tail rides in your context every turn). For facts worth keeping: preferences, decisions, how-tos — not transient state.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "required": ["text"]}}},
]
