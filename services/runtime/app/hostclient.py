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
        data = r.json() if r.content else {}
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


def _sanitize(messages: list[dict], max_messages: int = 200) -> list[dict]:
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
        out.append(m)
    return out[-max_messages:]


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
    tmp.write_text("\n".join(json.dumps(m) for m in messages) + "\n")
    os.replace(tmp, p)   # atomic: crash never truncates the transcript
