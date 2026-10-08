"""Soul & memory as markdown files (R50).

Every agent's workspace holds:
  SOUL.md     — voice & personality. Generated once from the assigned
                persona (R49), then owned by the agent (and the operator:
                edit the file, it applies next turn). Injected EVERY turn.
  MEMORY.md   — durable notes, append-oriented. Tail-injected each turn.
  notes/*.md  — working material. NEVER injected; searched on demand.

Injection budgets (chars): SOUL 4096, MEMORY tail 8192. Files on disk are
never truncated — only the injected copy is capped, and an oversized file
produces a visible marker so the agent (or operator) trims it.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

SOUL_MAX = 4096
MEMORY_TAIL = 8192
NOTE_MAX = 4000
SEARCH_HITS = 10

SOUL_TEMPLATE = """# {name}

{lede}

## Voice
{voice}

## Values
- Say the true thing, kindly or bluntly — but say it.
- Small, finished work beats grand unfinished plans.
- When stuck more than a few minutes, tell someone instead of stalling.
- Admit uncertainty fast; a wrong confident claim is the worst outcome.

---
*This file is my voice — who I am at work. I keep it current with ws.write
as I learn how I work best. (Operator: edit freely; applies next turn.)*
"""


def _persona(actor: dict) -> dict:
    p = actor.get("persona") or {}
    return {
        "name": p.get("persona_name") or actor.get("display_name")
        or actor.get("username") or "agent",
        "traits": p.get("persona_traits") or [],
        "style": p.get("persona_style") or "",
    }


def _soul_markdown(actor: dict) -> str:
    p = _persona(actor)
    traits = p["traits"]
    lede = (f"{p['name']} — {'; '.join(traits)}."
            if traits else f"{p['name']}.")
    voice_lines = [f"- {' and '.join(traits)}." if traits else "- Even-keeled."]
    if p["style"]:
        voice_lines.append(f"- {p['style']}.")
    voice_lines.append("- Short answers by default; depth when asked.")
    return SOUL_TEMPLATE.format(name=p["name"], lede=lede,
                                voice="\n".join(voice_lines))


def ensure_soul_files(workspace: str, actor: dict) -> None:
    """Create SOUL.md / MEMORY.md on first boot. Never overwrite."""
    os.makedirs(os.path.join(workspace, "notes"), exist_ok=True)
    soul = os.path.join(workspace, "SOUL.md")
    if not os.path.exists(soul):
        with open(soul, "w") as f:
            f.write(_soul_markdown(actor))
    mem = os.path.join(workspace, "MEMORY.md")
    if not os.path.exists(mem):
        with open(mem, "w") as f:
            f.write("# Memory\n\nDurable notes to myself. Newest at the "
                    "bottom; the tail rides along in my context.\n\n")


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


_SOUL_RE = re.compile(r"<soul>.*?</soul>\s*", re.DOTALL)
_MEMORY_RE = re.compile(r"<memory>.*?</memory>\s*", re.DOTALL)
# R70's episodic block is a SEPARATE top-level part — it must be stripped
# symmetrically or every _refresh_soul() step accumulates another copy
# (issue #4: ~50 copies per 50-step turn, unbounded system-prompt growth)
_EPISODIC_RE = re.compile(
    r"<episodic-memory[^>]*>.*?</episodic-memory>\s*", re.DOTALL)


def strip_soul(body: str) -> str:
    """Remove previously injected soul/memory/episodic blocks from a
    system body (must stay symmetric with soul_block's output)."""
    body = _SOUL_RE.sub("", body)
    body = _MEMORY_RE.sub("", body)
    body = _EPISODIC_RE.sub("", body)
    return body.strip()


def soul_block(workspace: str) -> str:
    """The injected identity+memory block for the system prompt."""
    soul = _read(os.path.join(workspace, "SOUL.md"))
    parts = []
    if soul:
        if len(soul) > SOUL_MAX:
            soul = soul[:SOUL_MAX] + "\n…[SOUL.md truncated for context — trim it]"
        parts.append(f"<soul>\n{soul}\n</soul>")
    mem = _read(os.path.join(workspace, "MEMORY.md"))
    if mem.strip():
        if len(mem) > MEMORY_TAIL:
            mem = "…[older notes cut — read MEMORY.md for full history]\n" \
                  + mem[-MEMORY_TAIL:]
        parts.append(f"<memory>\n{mem}\n</memory>")
    # R70: fractal quadtree episodic memory — newest entry of EVERY level
    # rides in the system prompt (fine recent + coarse ancient at once)
    try:
        from . import compact as _compact
        qt = _compact.qtree_soul_extension(workspace)
        if qt:
            parts.append(qt)
    except Exception:  # noqa: BLE001 — memory must never break the boot
        pass
    return "\n\n".join(parts)


def memory_append(workspace: str, text: str) -> str:
    """Append a timestamped note to MEMORY.md (the memory.write backend)."""
    text = text.strip()[:NOTE_MAX]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(workspace, "MEMORY.md"), "a") as f:
        f.write(f"- [{ts}] {text}\n")
    return "remembered"


def memory_search(workspace: str, q: str) -> str:
    """Regex search over MEMORY.md + notes/*.md (the agent's own files)."""
    rx = re.compile(q, re.IGNORECASE)
    hits: list[str] = []
    targets = [("MEMORY.md", os.path.join(workspace, "MEMORY.md"))]
    ndir = os.path.join(workspace, "notes")
    if os.path.isdir(ndir):
        for fn in sorted(os.listdir(ndir)):
            if fn.endswith(".md"):
                targets.append((f"notes/{fn}", os.path.join(ndir, fn)))
    for label, path in targets:
        for i, line in enumerate(_read(path).splitlines(), 1):
            if line.strip() and rx.search(line):
                hits.append(f"{label}:{i}: {line.strip()[:200]}")
                if len(hits) >= SEARCH_HITS:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "no memories match"
