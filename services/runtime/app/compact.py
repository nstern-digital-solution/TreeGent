"""R70 wiring: quadtree compaction inside the agent loop.

Compaction points:
  1. TURN END — if the chatlog crosses qtree.COMPACT_TRIGGER_CHARS, the
     OLDEST non-system messages (down to half the trigger) are summarized
     into the quadtree BEFORE the context is trimmed, then removed from
     the live messages. Nothing silently disappears anymore.
  2. CONTEXT BLOCK — soul_block() appends the qtree view (newest entry of
     every level), so every generation carries all coarseness levels.
  3. RESTORE — _sanitize already tail-caps at 200 messages; the qtree
     context block covers whatever fell outside that window long-term.

Summary generations go through the same proxy job queue (class "task" —
auxiliary, never agent-class billing), single-shot, tools disabled.
"""

from __future__ import annotations


# ---- 1. turn-end compaction -------------------------------------------------

async def maybe_compact(agent) -> dict:
    """Called in the turn's finally block. Returns stats (may be empty)."""
    from . import qtree
    msgs = agent.messages
    if not qtree.needs_compaction(msgs):
        return {}
    # Split off the oldest non-system messages, keeping the newest half of
    # whichever budget fired (chars or message count — count matters for
    # short-message sessions that would otherwise hit _sanitize's 200-msg
    # tail-cap without ever triggering the char path).
    non_sys = [(i, len(m.get("content") or ""))
               for i, m in enumerate(msgs) if m.get("role") != "system"]
    total = sum(n for _, n in non_sys)
    budget_keep_chars = qtree.COMPACT_TRIGGER_CHARS // 2
    budget_keep_msgs = qtree.COMPACT_TRIGGER_MSGS // 2
    drop_idx: set[int] = set()
    acc_chars = 0
    acc_msgs = 0
    for i, n in non_sys:               # oldest first
        if (total - acc_chars <= budget_keep_chars
                and len(non_sys) - acc_msgs <= budget_keep_msgs):
            break
        drop_idx.add(i)
        acc_chars += n
        acc_msgs += 1
    if not drop_idx:
        return {}
    dropped = [m for i, m in enumerate(msgs) if i in drop_idx]
    agent.messages = [m for i, m in enumerate(msgs) if i not in drop_idx]

    stats = {}
    try:
        stats = await qtree.record_compaction(
            agent.workspace, dropped, lambda p: _gen(agent, p))
        # R70: the qtree view changed -> regenerate the system prompt so
        # the next generation sees the new entry
        agent._regen_system_prompt()
    except Exception as e:  # noqa: BLE001 — never fail the turn for memory
        print(f"[qtree] compaction failed, raw kept: {e}")
    return stats


async def _gen(agent, prompt: str) -> str:
    """Single-shot summary generation via the proxy (class "task")."""
    import asyncio
    from . import config
    settings = config.settings
    job = {"class_name": "task", "reason": "compaction",
           "messages": [{"role": "user", "content": prompt}],
           "tools": [], "max_tokens": 2048}
    sub = await agent.svcs._call(settings.proxy_url, "/v1/jobs", "POST",
                                 body=job)
    j = {}
    for _ in range(120):   # up to 4 min
        j = await agent.svcs._call(settings.proxy_url,
                                   f"/v1/jobs/{sub['job_id']}")
        if j["status"] in ("done", "failed"):
            break
        await asyncio.sleep(2)
    if j.get("status") != "done":
        raise RuntimeError(f"summary job {j.get('error', 'failed')}")
    return j["result"]["choices"][0]["message"]["content"]


# ---- 2. context block in the system prompt ---------------------------------

def qtree_soul_extension(workspace: str) -> str:
    """Appended to <memory> in soul_block — all coarseness levels."""
    from . import qtree
    view = qtree.context_block(workspace)
    if not view:
        return ""
    return (f"<episodic-memory levels=\"{view.count(chr(10)) + 1}\">\n"
            f"{view}\n</episodic-memory>")
