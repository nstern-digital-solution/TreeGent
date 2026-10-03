"""Agent tools (R42). Every tool call carries the agent's OWN key; services
derive caller identity from the key server-side (R45) — an agent can never
act as anyone else. Untrusted content (mail bodies, chat messages) enters
the transcript only as clearly-delimited DATA inside tool results."""
import asyncio
import json
import os
import re

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
        headers = {"X-Agent-Key": self.key,
                   "X-Service-Token": settings.service_token}
        # chat still runs the M1 trusted-caller model (R45 migration pending)
        if self.agent_id:
            headers["X-Actor-Id"] = self.agent_id
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
    def __init__(self, agent_id: str, workspace: str, svcs: Services):
        self.agent_id = agent_id
        self.workspace = workspace
        self.svcs = svcs



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
    """Run a shell command in the agent workspace (sandboxed cwd, timeout)."""
    cmd = args["cmd"]
    if len(cmd) > 2000:
        return "ERROR: command too long"
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
    from .config import db, memories
    rx = {"$regex": args["q"], "$options": "i"}
    hits = [m async for m in memories.find(
        {"agent_id": ctx.agent_id,
         "$or": [{"text": rx}]}).sort("ts", -1).limit(10)]
    if not hits:
        return "no memories match"
    return "\n".join(f"[{h['ts'].strftime('%m-%d %H:%M')}] {h['text'][:200]}"
                     for h in hits)


async def t_memory_write(ctx: ToolContext, args: dict) -> str:
    from datetime import datetime, timezone
    from .config import memories
    await memories.insert_one({
        "_id": f"mem_{os.urandom(6).hex()}",
        "agent_id": ctx.agent_id,
        "text": args["text"][:4000],
        "ts": datetime.now(timezone.utc)})
    return "remembered"


TOOLS: dict = {
    "chat.send": t_chat_send,
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
        "name": "memory.search", "description": "search your own memory notes",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}, "required": ["q"]}}},
    {
        "name": "memory.write", "description": "save a note to your own memory",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "required": ["text"]}}},
]
