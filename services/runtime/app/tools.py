"""Agent tools (R42). Every tool call carries the agent's OWN key; services
derive caller identity from the key server-side (R45) — an agent can never
act as anyone else. Untrusted content (mail bodies, chat messages) enters
the transcript only as clearly-delimited DATA inside tool results."""
import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit
import time

import httpx

from .config import settings


# ---------------- service client (key-authenticated) ----------------

class Services:
    def __init__(self, agent_key: str, agent_id: str = ""):
        self.key = agent_key
        self.agent_id = agent_id  # set by supervisor (key->agent from DB)

    def _headers(self) -> dict:
        # R45: the agent key IS the agent-tier credential. The shared
        # service token rides along ONLY when the runtime itself runs on
        # the trusted central box (settings.service_token is empty on
        # agent hosts — provisioning writes none), letting central-mode
        # calls reach service-tier endpoints (inbox polling). On agent
        # hosts the header is absent and R56 host-tier endpoints are used.
        headers = {"X-Agent-Key": self.key}
        # R69 fix: hosted runtimes must NEVER attach the per-host daemon
        # token (it authenticates the box's own /internal API, not chat).
        # On central mode the shared token is still sent as before.
        if settings.service_token and not settings.host_key:
            headers["X-Service-Token"] = settings.service_token
        return headers

    async def _call(self, base: str, path: str, method: str = "GET",
                    body: dict | None = None, params: dict | None = None,
                    timeout: float = 60.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, f"{base}{path}",
                                headers=self._headers(),
                                json=body, params=params)
        # R72b: r.json() before the status check masked non-JSON error
        # bodies (plain-text 500s) as JSONDecodeError. Parse defensively.
        try:
            data = r.json() if r.content else {}
        except Exception:  # noqa: BLE001 — non-JSON body
            data = {"raw": r.text[:300]}
        if r.status_code >= 400:
            raise RuntimeError(f"service {r.status_code}: "
                               f"{json.dumps(data)[:300]}")
        return data

    async def _call_bytes(self, base: str, path: str,
                          params: dict | None = None,
                          timeout: float = 120.0) -> bytes:
        """GET a binary body (files.download sends raw bytes)."""
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{base}{path}", headers=self._headers(),
                            params=params)
        if r.status_code >= 400:
            raise RuntimeError(f"service {r.status_code}: {r.text[:300]}")
        return r.content

    async def _upload(self, base: str, path: str, filename: str, data: bytes,
                      content_type: str = "application/octet-stream",
                      params: dict | None = None,
                      timeout: float = 300.0) -> dict:
        """multipart upload (files.push)."""
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{base}{path}", headers=self._headers(),
                             params=params,
                             files={"file": (filename, data, content_type)})
        try:
            resp = r.json() if r.content else {}
        except Exception:  # noqa: BLE001 — non-JSON body
            resp = {"raw": r.text[:300]}
        if r.status_code >= 400:
            raise RuntimeError(f"service {r.status_code}: "
                               f"{json.dumps(resp)[:300]}")
        return resp


# ---------------- path guard for workspace files ----------------

def safe_ws_path(workspace: str, rel: str) -> str:
    """Resolve rel inside workspace; refuse escapes."""
    root = os.path.realpath(workspace)
    p = os.path.realpath(os.path.join(root, rel))
    if not (p == root or p.startswith(root + os.sep)):
        raise PermissionError(f"path escapes workspace: {rel!r}")
    return p


def _fmt_size(n) -> str:
    """Human byte size for attachment lines: '13 KB', '1.2 MB'."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# ---------------- SSRF guard (R68): no fetches into private space ---------

def _url_allowed(host: str) -> bool:
    """True when EVERY address the host resolves to is public. Rejects
    loopback, link-local (incl. 169.254.169.254 metadata), RFC1918,
    unique-local and other reserved space. TG_WEB_FETCH_ALLOW_PRIVATE=1
    is the dev/rehearsal escape hatch (the rehearsal fetches localhost)."""
    if os.environ.get("TG_WEB_FETCH_ALLOW_PRIVATE", "") == "1":
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False          # unresolvable -> deny (fail closed)
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return False
    return True


def _fetch_url_allowed(url: str) -> str | None:
    """Pre-request SSRF check for a URL: ERROR string when denied, else None."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return "ERROR: malformed URL"
    if not host:
        return "ERROR: URL has no host"
    if not _url_allowed(host):
        return (f"ERROR: fetch denied — {host} resolves inside "
                f"loopback/link-local/private space (SSRF guard; "
                f"TG_WEB_FETCH_ALLOW_PRIVATE=1 allows it on a dev box)")
    return None


def _nav_url_allowed(url: str) -> tuple[bool, str]:
    """H4: per-navigation SSRF re-validation — (allowed, reason). Same
    deny-list as _url_allowed (unchanged), applied to EVERY navigation the
    browser makes: the explicit open, redirect hops Chromium follows
    INTERNALLY (the framenavigated hook sees only the post-redirect URL),
    and click-driven navigations. about:blank is the browser's neutral
    state and performs no fetch, so it passes; everything else must be
    http(s) and resolve to public space only (fail closed)."""
    if url.startswith("about:blank"):
        return True, ""
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname
    except ValueError:
        return False, "malformed URL"
    if scheme not in ("http", "https"):
        return False, "only http(s) URLs are allowed"
    if not host:
        return False, "URL has no host"
    if not _url_allowed(host):
        return False, (f"{host} resolves inside loopback/link-local/private "
                       f"space")
    return True, ""


# ---------------- tool implementations ----------------

class ToolContext:
    def __init__(self, agent_id: str, workspace: str, svcs: Services,
                 host_client=None, username: str = ""):
        self.agent_id = agent_id
        self.workspace = workspace
        self.svcs = svcs
        self.host_client = host_client   # hosted mode: host-tier client
        self.username = username         # actor username (files.push subtree)



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
        # R69 fix: values were concatenated unquoted — a sender containing
        # & or = corrupted the query
        from urllib.parse import quote
        qs = "&".join(f"{k}={quote(str(v), safe='')}"
                      for k, v in params.items())
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


async def t_mail_read(ctx: ToolContext, args: dict) -> str:
    """Read your mail. mail.read with no args = your own mailbox overview
    (address + latest messages). Optional: mailbox=<shared address you are
    a member of>, limit=<int>, unread_only=<bool>. Also answers 'what is
    my email address' — the overview lists it."""
    from urllib.parse import quote
    params: dict = {"limit": str(max(1, min(int(args.get("limit", 10)), 50)))}
    # 1) which mailboxes can I read?
    boxes = await ctx.svcs._call(settings.mail_url, "/mailboxes")
    mine = [b for b in boxes if b.get("kind") == "personal"
            and b.get("owner") == ctx.agent_id]
    shared = [b for b in boxes if b.get("kind") == "shared"]
    if args.get("mailbox"):
        want = str(args["mailbox"]).strip().lower()
        cand = [b for b in mine + shared
                if b.get("address", "").lower() == want]
        if not cand:
            # parens matter: `A + B if mine else "none"` parses as
            # `(A + B) if mine else "none"` and collapsed the whole message
            # to 'none' whenever no personal box existed (#26)
            return (f"ERROR: no readable mailbox {want!r}; you own "
                    + (", ".join(b["address"] for b in mine) if mine
                       else "none"))
        target = cand[0]
    else:
        if not mine and not shared:
            return ("no mailbox yet for this agent — the mail service "
                    "creates it at startup from your name + TG_MAIL_DOMAIN; "
                    "if it stays missing, ask your operator to check "
                    "TG_MAIL_DOMAIN and restart the mail service")
        target = mine[0] if mine else shared[0]
    msgs = await ctx.svcs._call(
        settings.mail_url,
        f"/mailboxes/{quote(target['id'])}/messages", params=params)
    lines = [f"mailbox: {target['address']} ({target['kind']})"]
    if args.get("unread_only"):
        msgs = [m for m in msgs if m.get("direction") == "in"]
    for m in msgs:
        d = "→" if m.get("direction") == "out" else "←"
        line = (f"[{m.get('ts', '')}] {d} from {m.get('from') or '?'} "
                f"| {m.get('subject', '')} "
                f"(id: {m.get('id') or m.get('_id') or '?'})")
        # issue #60 (notify-only): attachments are LISTED, never downloaded
        # here — mail.save_attachment fetches one on explicit request
        for a in m.get("attachments") or []:
            line += (f" | attach: {a.get('filename') or '?'} "
                     f"({_fmt_size(a.get('size'))}, "
                     f"{a.get('content_type') or 'application/octet-stream'})")
        lines.append(line)
        body = (m.get("text") or "")[:500]
        if body:
            lines.append(f"    {body}")
    if len(lines) == 1:
        lines.append("(no messages)")
    return "\n".join(lines)


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


async def t_mail_save_attachment(ctx: ToolContext, args: dict) -> str:
    """Save ONE mail attachment into your workspace (issue #60). Nothing is
    ever downloaded automatically — this tool is the ONLY way attachment
    bytes reach your workspace. mail=<mail row id from mail.read>,
    filename=<attachment name>, to=<workspace path> (default:
    attachments/<filename>), overwrite=true to replace an existing file."""
    from urllib.parse import quote
    mail_id = str(args.get("mail") or "").strip()
    name = str(args.get("filename") or "").strip()
    if not mail_id or not name:
        return ("ERROR: need 'mail' (mail row id from mail.read) and "
                f"'filename'; got keys {sorted(args)}")
    # find the mail row through the messages endpoints (own/shared scope
    # enforced server-side — unreadable mail is simply not found here)
    try:
        boxes = await ctx.svcs._call(settings.mail_url, "/mailboxes")
    except RuntimeError as e:
        return f"ERROR: {e}"
    msg, mbx_id = None, ""
    for b in boxes or []:
        msgs = await ctx.svcs._call(
            settings.mail_url,
            f"/mailboxes/{quote(str(b.get('id')))}/messages",
            params={"limit": "200"})
        hit = [m for m in (msgs or []) if m.get("id") == mail_id]
        if hit:
            msg, mbx_id = hit[0], str(b.get("id"))
            break
    if not msg:
        return (f"ERROR: no readable mail message {mail_id!r} — pass the "
                "mail row id shown by mail.read")
    atts = msg.get("attachments") or []
    cands = [a for a in atts if (a.get("filename") or "") == name]
    if len(cands) > 1:
        listing = ", ".join(
            f"{a.get('filename')!r} (id {a.get('id')}, {_fmt_size(a.get('size'))}, "
            f"{a.get('content_type')})" for a in cands)
        return (f"ERROR: {name!r} matches {len(cands)} attachments on this "
                f"mail — candidates: {listing}")
    if not cands:
        # the ambiguity error lists ids — accept one so it is resolvable
        cands = [a for a in atts if a.get("id") == name]
    if not cands:
        avail = ", ".join(f"{a.get('filename')!r} (id {a.get('id')})"
                          for a in atts) or "none"
        return (f"ERROR: no attachment {name!r} on this mail — "
                f"available: {avail}")
    att = cands[0]
    to = (str(args.get("to") or "").strip()
          or os.path.join("attachments",
                          os.path.basename(att.get("filename") or "attachment")))
    try:
        dest = safe_ws_path(ctx.workspace, to)
    except PermissionError as e:
        return f"ERROR: {e}"
    if os.path.exists(dest) and not args.get("overwrite"):
        return (f"ERROR: {to} already exists in the workspace — pass "
                f"overwrite=true to replace it")
    try:
        data = await ctx.svcs._call_bytes(
            settings.mail_url,
            f"/mailboxes/{quote(mbx_id)}/messages/{quote(mail_id)}"
            f"/attachments/{quote(str(att.get('id')))}")
    except RuntimeError as e:
        return f"ERROR: {e}"
    os.makedirs(os.path.dirname(dest) or ctx.workspace, exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    return f"saved {to} ({len(data)} bytes)"


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


# ---------------- shared file system (issue #51 D) ----------------

async def t_files_ls(ctx: ToolContext, args: dict) -> str:
    """List one directory of the shared file system: the folders and files
    visible to you there. path defaults to "/"."""
    path = str(args.get("path") or "/").strip() or "/"
    r = await ctx.svcs._call(settings.files_url, "/files",
                             params={"path": path})
    folders = (r or {}).get("folders", []) if isinstance(r, dict) else []
    files = (r or {}).get("files", []) if isinstance(r, dict) else []
    lines = [f"[folder] {d.get('name', '?')}" for d in folders]
    lines += [f"[file] {f.get('name', '?')} ({f.get('size') or 0} bytes)"
              for f in files]
    return "\n".join(lines) or "(empty)"


async def t_files_pull(ctx: ToolContext, args: dict) -> str:
    """Download a shared file into your workspace. path=<absolute file path
    in the shared file system>, to=<workspace path> (default: the file
    name). Never overwrites: pass overwrite=true to replace."""
    path = str(args.get("path") or "").strip()
    if not path:
        return "ERROR: need 'path' (absolute file path in the shared file system)"
    parent, _, name = path.rstrip("/").rpartition("/")
    parent = parent or "/"
    if not name:
        return f"ERROR: {path!r} does not name a file"
    try:
        listing = await ctx.svcs._call(settings.files_url, "/files",
                                       params={"path": parent})
    except RuntimeError as e:
        return f"ERROR: {e}"
    match = None
    for f in (listing or {}).get("files", []):
        if f.get("name") == name:
            match = f        # newest-ish: listing is sorted, first hit wins
    if not match:
        return f"ERROR: no visible file at {path}"
    to = str(args.get("to") or name)
    try:
        dest = safe_ws_path(ctx.workspace, to)
    except PermissionError as e:
        return f"ERROR: {e}"
    if os.path.exists(dest) and not args.get("overwrite"):
        return (f"ERROR: {to} already exists in the workspace — pass "
                f"overwrite=true to replace it")
    try:
        data = await ctx.svcs._call_bytes(settings.files_url,
                                          f"/files/{match['id']}/download")
    except RuntimeError as e:
        return f"ERROR: {e}"
    os.makedirs(os.path.dirname(dest) or ctx.workspace, exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    return f"pulled {path} -> {to} ({len(data)} bytes)"


async def t_files_push(ctx: ToolContext, args: dict) -> str:
    """Upload a workspace file into YOUR OWN subtree of the shared file
    system (/home/<you>/… — 100 MB cap). from=<workspace file>,
    to_path=<absolute destination file path>."""
    frm = str(args.get("from") or "").strip()
    to_path = str(args.get("to_path") or "").strip()
    if not frm or not to_path:
        return "ERROR: need 'from' (workspace file) and 'to_path' (absolute destination)"
    try:
        src = safe_ws_path(ctx.workspace, frm)
    except PermissionError as e:
        return f"ERROR: {e}"
    if not os.path.isfile(src):
        return f"ERROR: no workspace file {frm!r}"
    # own-subtree restriction (write side): /home/<username>/… only
    home = f"/home/{ctx.username}" if getattr(ctx, "username", "") else ""
    parent, _, name = to_path.rstrip("/").rpartition("/")
    parent = parent or "/"
    if (not home or not to_path.startswith(home + "/") or not name
            or any(s == ".." for s in to_path.split("/"))):
        return (f"ERROR: files.push is restricted to your own subtree "
                f"/home/{ctx.username or '<you>'}/… — got {to_path!r}")
    size = os.path.getsize(src)
    if size > 100 * 1024 * 1024:
        return "ERROR: file too large (100 MB cap)"
    with open(src, "rb") as fh:
        data = fh.read()
    try:
        r = await ctx.svcs._upload(settings.files_url, "/files", name, data,
                                   params={"path": parent})
    except RuntimeError as e:
        return f"ERROR: {e}"
    return f"pushed {frm} -> {r.get('path') or to_path} ({size} bytes)"


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
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as c:
        # R68: redirects are followed MANUALLY — every hop is re-validated
        # against the SSRF deny-list (auto-follow would let a public URL
        # bounce the request at 127.0.0.1/metadata and return the result)
        for _ in range(6):
            err = _fetch_url_allowed(url)
            if err:
                return err
            r = await c.get(url, headers={"User-Agent": "TreeGentAgent/0.1"})
            loc = r.headers.get("location")
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(str(r.url), loc)
                continue
            break
        else:
            return "ERROR: too many redirects"
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
    """R76: vector-rank the derived conclusions (top-k, premises shown)
    plus the labeled notes-grep over MEMORY.md/notes. NEVER errors — an
    unavailable embedding path degrades to notes-only with a one-line note."""
    from . import conclusions as C
    from . import soul as SOUL
    q = args.get("q") or ""
    try:
        k = int(args.get("k", 5))
    except (TypeError, ValueError):
        k = 5
    k = max(1, min(k, 20))
    parts: list[str] = []
    try:
        qvec = (await C.embed_texts(ctx.svcs, [q], timeout=10.0))[0]
        hits = C.search_conclusions(ctx.workspace, qvec, k)
        if hits:
            parts.extend(C.format_conclusion(h) for h in hits)
        else:
            parts.append("(no matching conclusions)")
    except Exception:  # noqa: BLE001 — degraded, never an error
        parts.append("(vector memory unavailable)")
    try:
        notes = SOUL.memory_search(ctx.workspace, q)
    except re.error as e:
        notes = f"ERROR: bad search regex: {e}"
    except Exception as e:  # noqa: BLE001 — the tool never errors
        notes = f"(notes grep unavailable: {e})"
    parts.append("(source: notes)")
    parts.append(notes)
    return "\n".join(parts)


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
# Rosa bug report 2026-10-06: the sync Playwright API is greenlet/thread-AFFINE —
# objects must be touched ONLY from the thread that created them. A bare
# asyncio.to_thread round-robins the pool, so every-other-call landed on the
# wrong thread: "Cannot switch to a different thread", or worse, silently
# "(empty page)" because _browser_state swallowed the error. Fix: ONE
# dedicated executor thread per workspace, for the context's lifetime.
_BROWSER_EXECS: dict[str, ThreadPoolExecutor] = {}


def _browser_exec(workspace: str) -> ThreadPoolExecutor:
    """The single thread this workspace's browser lives on."""
    ex = _BROWSER_EXECS.get(workspace)
    if ex is None:
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg-browse")
        _BROWSER_EXECS[workspace] = ex
    return ex


_BROWSER_LOCK = threading.Lock()     # guards _BROWSERS/_BROWSER_EXECS dicts only


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
        ex = _BROWSER_EXECS.pop(ws, None)
        if ex is not None:
            ex.shutdown(wait=False)


import atexit


def _browser_shutdown() -> None:
    """Rosa review #4: daemon exit must not leak Chromium processes."""
    with _BROWSER_LOCK:
        for ws in list(_BROWSERS):
            try:
                _BROWSERS.pop(ws)["pw"].stop()
            except Exception:  # noqa: BLE001
                pass
            ex = _BROWSER_EXECS.pop(ws, None)
            if ex is not None:
                ex.shutdown(wait=False)


atexit.register(_browser_shutdown)


# H4: per-navigation SSRF re-validation. The initial-URL check alone let a
# permitted page redirect (or a clicked link) into loopback/private space —
# Chromium follows redirects INTERNALLY and click navigations never touch the
# tool-side check. Probe-verified: framenavigated fires once per committed
# navigation, carrying the POST-redirect URL, and frame wrappers are
# identity-stable (frame is page.main_frame holds in the handler).
BROWSER_NAV_URL_CAP = 200             # bounded blocked-URL in error messages


def _nav_blank(page) -> None:
    """Blank the page so no disallowed content reaches a tool result.
    Best-effort, never raises. NOTE: a goto issued from inside a
    framenavigated handler is only QUEUED by the sync Playwright API and
    lands on a later event pump — the deterministic blank must happen on
    the main flow (here via _browser_state / the post-goto check)."""
    try:
        page.goto("about:blank")
    except Exception:  # noqa: BLE001
        pass


def _nav_block(state: dict, page, url: str) -> None:
    """Record the bounded 'navigation blocked' error in the browser state
    and blank the page. Never raises."""
    try:
        state["nav_blocked"] = (f"navigation blocked: "
                                f"{url[:BROWSER_NAV_URL_CAP]} not allowed")
    except Exception:  # noqa: BLE001
        pass
    _nav_blank(page)


def _install_nav_guard(page, state: dict) -> None:
    """Register the main-frame framenavigated hook: every committed
    navigation (redirect hops, click-driven, whatever) is re-validated
    against the SSRF deny-list. A disallowed URL is recorded in the browser
    state and the page is blanked. The handler NEVER raises into Playwright
    internals — everything is caught."""
    def on_frame_navigated(frame) -> None:
        try:
            try:
                main = (frame is getattr(page, "main_frame", None)
                        or getattr(frame, "parent_frame", None) is None)
            except Exception:  # noqa: BLE001 — ambiguous -> enforce (fail closed)
                main = True
            if not main:
                return                      # main frame only
            url = getattr(frame, "url", "") or ""
            ok, _reason = _nav_url_allowed(url)
            if ok:
                if url.startswith(("http://", "https://")):
                    state.pop("nav_blocked", None)   # real page -> retire error
                return
            _nav_block(state, page, url)
        except Exception:  # noqa: BLE001 — never throw into Playwright
            pass
    try:
        page.on("framenavigated", on_frame_navigated)
    except Exception:  # noqa: BLE001
        pass


def _browser_state(page, state: dict | None = None) -> str:
    """Small state dump after an action: where we are + readable text.
    A broken session must surface as ERROR — never '(empty page)'.
    H4: while a navigation is blocked, blank the page FIRST (the hook's own
    goto is queued, not immediate) so no disallowed content leaks into the
    result, and surface the bounded error above the dump."""
    if state and state.get("nav_blocked"):
        _nav_blank(page)
        try:
            title = page.title()
            text = page.locator("body").inner_text(timeout=5000)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()[:BROWSER_TEXT_CAP]
            url_line = page.url
        except Exception:  # noqa: BLE001 — the ERROR line is the payload
            title, text, url_line = "", "", "(blanked)"
        return (f"ERROR: {state['nav_blocked']}\n\n"
                f"url: {url_line}\ntitle: {title}\n\n{text or '(empty page)'}")
    title = page.title()
    text = page.locator("body").inner_text(timeout=5000)
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
    state = {"pw": pw, "ctx": bctx, "page": page}
    _install_nav_guard(page, state)   # H4: re-validate every navigation
    return state


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

        url = args.get("url") or ""
        if action == "open" or url:
            # R68 + H4: SSRF guard BEFORE any action carrying a URL
            if not re.match(r"^https?://", url):
                return "ERROR: http(s) URL required"
            err = _fetch_url_allowed(url)   # R68: SSRF guard before goto
            if err:
                return err
        if action == "open":
            page.goto(url, wait_until="load")
            # H4 belt+suspenders: goto follows redirects INTERNALLY — the
            # FINAL url must pass the same check even if the hook missed it
            ok, _reason = _nav_url_allowed(page.url)
            if not ok:
                _nav_block(state, page, page.url)
            return _browser_state(page, state)
        if action == "text":
            return _browser_state(page, state)
        if action == "click":
            sel = args.get("selector") or ""
            if not sel:
                return "ERROR: need 'selector'"
            page.click(sel)
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:  # noqa: BLE001 — SPA clicks never fire load
                pass
            return _browser_state(page, state)
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
            return _browser_state(page, state)
        if action == "screenshot":
            rel = args.get("path") or "screenshot.png"
            abs_p = safe_ws_path(workspace, rel)
            os.makedirs(os.path.dirname(abs_p) or workspace, exist_ok=True)
            if state.get("nav_blocked"):
                _nav_blank(page)   # H4: never snapshot a disallowed page
            page.screenshot(path=abs_p, full_page=True)
            return (f"saved {rel} ({os.path.getsize(abs_p)} bytes) "
                    f"in workspace")
        if action == "close":
            try:
                state["ctx"].close()
                state["pw"].stop()
            finally:
                _BROWSERS.pop(workspace, None)
                ex = _BROWSER_EXECS.pop(workspace, None)
                if ex is not None:
                    ex.shutdown(wait=False)
            return "browser closed"
        return (f"ERROR: unknown browser action {action!r} — use open, text, "
                f"click, type, screenshot or close")


async def t_browser(ctx: ToolContext, args: dict) -> str:
    """Drive the agent's headless browser. action=open|text|click|type|
    screenshot|close. One persistent session per agent (state survives
    across turns)."""
    action = args.get("action") or ""
    if action not in ("open", "text", "click", "type", "screenshot", "close"):
        return (f"ERROR: need 'action' = open | text | click | type | "
                f"screenshot | close; got {action!r}")
    if action == "open" or args.get("url"):
        # R68 + H4: deny BEFORE the browser even launches (the sync path
        # would spin up Chromium first and only then validate) — and for
        # EVERY action carrying a URL, not just open
        err = _fetch_url_allowed(args.get("url") or "")
        if err:
            return err
    try:
        # blocking Playwright work stays off the async loop; 45s hard backstop
        # above the ~30s per-action Playwright timeouts
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                _browser_exec(ctx.workspace), _sync_browser_action,
                ctx.workspace, action, args),
            timeout=45)
    except asyncio.TimeoutError:
        return "ERROR: browser action timed out (45s)"


TOOLS: dict = {
    "chat.send": t_chat_send,
    "chat.check": t_chat_check,
    "mail.send": t_mail_send,
    "mail.read": t_mail_read,
    "mail.save_attachment": t_mail_save_attachment,
    "mail.check": t_mail_check,
    "approvals.decide": t_approval_decide,
    "secrets.list": t_secrets_list,
    "secrets.read": t_secret_read,
    "secrets.write": t_secret_write,
    "files.ls": t_files_ls,
    "files.pull": t_files_pull,
    "files.push": t_files_push,
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
        "name": "mail.read",
        "description": "read your mailbox: your address + latest messages. Optional mailbox=<shared address you belong to>, limit, unread_only. Use this to learn your own email address.",
        "parameters": {"type": "object", "properties": {
            "mailbox": {"type": "string", "description": "shared mailbox address (optional; default = your personal one)"},
            "limit": {"type": "integer", "description": "max messages (default 10)"},
            "unread_only": {"type": "boolean", "description": "only incoming"}}}},
    {
        "name": "mail.save_attachment",
        "description": "save ONE mail attachment into your workspace. Attachments are never downloaded automatically — mail.read lists them ('| attach: name (size, type)') and this tool is the only way the bytes reach your workspace. Resolve by filename (or attachment id when two attachments share a name); to defaults to attachments/<filename>; refuses to overwrite unless overwrite=true.",
        "parameters": {"type": "object", "properties": {
            "mail": {"type": "string", "description": "mail row id (from mail.read)"},
            "filename": {"type": "string", "description": "attachment filename (or attachment id)"},
            "to": {"type": "string", "description": "workspace path to write (default: attachments/<filename>)"},
            "overwrite": {"type": "boolean"},
            "required": ["mail", "filename"]}}},
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
        "name": "files.ls",
        "description": "list a directory of the shared file system: the "
                       "folders and files visible to you there (path "
                       "defaults to \"/\")",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "absolute directory path"}}}},
    {
        "name": "files.pull",
        "description": "download a shared file into your workspace. Refuses "
                       "to overwrite an existing workspace file unless "
                       "overwrite=true.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "absolute path of the file in the shared file system"},
            "to": {"type": "string",
                   "description": "workspace path to write (default: the file name)"},
            "overwrite": {"type": "boolean"},
            "required": ["path"]}}},
    {
        "name": "files.push",
        "description": "upload a workspace file into YOUR OWN subtree of the "
                       "shared file system (/home/<you>/…, 100 MB cap)",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "string", "description": "workspace file path"},
            "to_path": {"type": "string",
                        "description": "absolute destination file path inside your own subtree"},
            "required": ["from", "to_path"]}}},
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
        "name": "memory.search", "description": "search your long-term memory: derived conclusions ranked by meaning (vector top-k, with their premises) plus a regex grep over MEMORY.md and notes/*.md (labeled 'source: notes')",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "word, phrase or regex"},
            "k": {"type": "integer", "description": "max conclusions to return (default 5, max 20)"},
            "required": ["q"]}}},
    {
        "name": "memory.write", "description": "append a durable note to MEMORY.md (timestamped, tail rides in your context every turn). For facts worth keeping: preferences, decisions, how-tos — not transient state.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "required": ["text"]}}},
]
