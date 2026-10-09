"""R56 hosted-runtime data client.

Agent hosts hold NO Mongo credentials. This module is the hosted
runtime's ONLY view of the world:

  - poll chat /internal/host/agents        (own agents + current keys)
  - fetch /internal/host/pending/<aid>     (inbox + wakes for that agent)
  - post  /internal/host/delivered/<aid>   (exactly the rendered ids)
  - post  /internal/host/turn/<aid>        (turn summary for the viewer)
  - record generation via proxy /v1/jobs   (X-Agent-Key, unchanged)

Transcripts live on THIS box, in the agent workspace (sessions/*.jsonl),
one line per message. The old TG_MONGO_URL path is gone.
"""
from __future__ import annotations

import json
import re
import sys
import os
from pathlib import Path

import httpx

from .config import settings


class HostClient:
    """HTTPS-only accessor for a hosted runtime."""

    def __init__(self, host_id: str, host_key: str):
        if not host_id or not host_key:
            raise RuntimeError(
                "hosted runtime requires TG_RUNTIME_HOST_ID and "
                "TG_RUNTIME_HOST_KEY (from provisioning) — refusing to "
                "start without them")
        self.host_id = host_id
        self.host_key = host_key
        self._headers = {"X-Host-Id": host_id, "X-Host-Key": host_key}

    # ---- transport ----

    async def _call(self, method: str, path: str, body: dict | None = None,
                    timeout: float = 30.0) -> dict:
        url = settings.chat_url.rstrip("/") + path
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, url, headers=self._headers,
                                json=body)
        # R72b: parse BEFORE the status check used to turn any non-JSON
        # error body (plain-text 500 from a crashing hop) into
        # JSONDecodeError — masking the real status. Parse defensively.
        try:
            data = r.json() if r.content else {}
        except Exception:  # noqa: BLE001 — non-JSON body
            data = {"raw": r.text[:300]}
        if r.status_code == 401:
            raise RuntimeError("host credentials rejected — re-provision "
                               "this host (key rotated or host removed)")
        if r.status_code >= 400:
            raise RuntimeError(f"chat {r.status_code}: "
                               f"{json.dumps(data)[:300]}")
        return data

    # ---- own agents + keys ----

    async def agents(self) -> list[dict]:
        return (await self._call("GET", "/internal/host/agents")).get(
            "agents", [])

    # ---- pending work for one agent ----

    async def pending(self, agent_id: str) -> dict:
        return await self._call("GET", f"/internal/host/pending/{agent_id}")

    async def delivered(self, agent_id: str, inbox_ids: list[str],
                        wake_ids: list[str]) -> None:
        await self._call("POST", f"/internal/host/delivered/{agent_id}",
                         {"inbox_ids": inbox_ids, "wake_ids": wake_ids})

    async def turn_report(self, agent_id: str, report: dict) -> None:
        await self._call("POST", f"/internal/host/turn/{agent_id}", report)

    async def push_transcript(self, agent_id: str, lines: list[dict]) -> None:
        """R57: ship this turn's transcript lines to central for the
        session explorer (bounded: content capped server-side too)."""
        if not lines:
            return
        await self._call("POST", f"/internal/host/transcript/{agent_id}",
                         lines, timeout=60.0)


# ---------------- local transcript storage (workspace files) ----------------

def session_path(workspace: str) -> Path:
    p = Path(workspace) / "sessions" / "current.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# Issue #11: model echoes of injected blocks (the 393KB assistant echo).
# Same markers soul.strip_soul removes from the system body — historical
# echoes ride in conversation history, which strip_soul never touches.
_ECHO_RES = (
    re.compile(r"<soul>.*?</soul>\s*", re.DOTALL),
    re.compile(r"<memory>.*?</memory>\s*", re.DOTALL),
    re.compile(r"<episodic-memory[^>]*>.*?</episodic-memory>\s*", re.DOTALL),
)
_MSG_CAP = 20_000      # a single message must never own the context window


def _bound_content(content: str) -> str:
    """Scrub echoed injection blocks and cap giant non-system content."""
    if not isinstance(content, str):
        return content
    scrubbed = content
    for rx in _ECHO_RES:
        scrubbed = rx.sub("", scrubbed)
    if scrubbed != content:
        content = scrubbed.strip() + "\n[scrubbed echo]"
    if len(content) > _MSG_CAP:
        cut = len(content) - 15_000    # keep head 10k + tail 5k
        content = (content[:10_000] + "\n[... truncated %d chars ...]\n" % cut
                   + content[-5_000:])
    return content


def _sanitize(messages: list[dict], max_messages: int = 500) -> list[dict]:
    """Heal the failure-storm bloat: a dead provider used to leave
    hundreds of identical user lines (re-appended unread backlogs and
    '[generation failed]' retry stubs). Drop failure stubs, keep only
    the FIRST occurrence of each distinct user message (a re-delivered
    old message between newer ones would otherwise be answered AGAIN),
    keep the newest tail."""
    seen_users: set[str] = set()
    out = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            if content.startswith("[generation failed"):
                continue        # retry noise, never real conversation
            if content in seen_users:
                continue        # duplicate delivery (re-ack race / spin)
            seen_users.add(content)
        if role != "system":
            # Issue #11: scrub echoed injection blocks + cap giant content
            # (the system message keeps its own strip_soul handling in loop).
            bounded = _bound_content(content)
            if bounded != content:
                m = dict(m, content=bounded)
        out.append(m)
    # R68: extract the system prompt FIRST, then tail-cap the REST. The old
    # order (tail-cap, then find system in the tail) dropped the system
    # message entirely on 200+ message sessions — and R50 regeneration only
    # replaces a LEADING system message, so the soul-refresh path then
    # rewrote a conversation message at index 0 instead.
    system_msg = out[0] if out and out[0].get("role") == "system" else None
    body = out[1:] if system_msg is not None else out
    room = max_messages - (1 if system_msg is not None else 0)
    tail = body[-room:] if room > 0 else []
    # Context budget: cap TOTAL chars so accumulation without compaction
    # can't explode again. The SYSTEM prompt is never elided; the rest is
    # trimmed oldest-first until the non-system content fits the budget.
    # 2M chars ~ 500k tokens: comfortably above the 1.2M-char compaction
    # trigger (R73) — this ceiling only catches a compaction failure.
    budget = 2_000_000
    syslen = len(system_msg.get("content") or "") if system_msg else 0
    pre_budget = len(tail)
    while (sum(len(m.get("content") or "") for m in tail)
           + syslen) > budget and len(tail) > 2:
        tail = tail[1:]
    if len(tail) < pre_budget:
        print("[context budget] dropped %d oldest message(s) to fit %d chars"
              % (pre_budget - len(tail), budget), file=sys.stderr)
    return ([system_msg] if system_msg is not None else []) + tail


def load_messages(workspace: str) -> list[dict]:
    p = session_path(workspace)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return _sanitize(out)


def save_messages(workspace: str, messages: list[dict]) -> None:
    p = session_path(workspace)
    tmp = p.with_suffix(".tmp")
    # Issue #11: bound what we persist too — an echo produced THIS turn
    # must not land on disk at full size (load path scrubs old victims).
    lines = []
    for m in messages:
        if m.get("role") != "system":
            content = m.get("content") or ""
            bounded = _bound_content(content)
            if bounded != content:
                m = dict(m, content=bounded)
        lines.append(json.dumps(m))
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, p)   # atomic: crash never truncates the transcript
