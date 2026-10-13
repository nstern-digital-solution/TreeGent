"""R70 wiring: quadtree compaction inside the agent loop.

Compaction points:
  1. TURN END — when qtree.needs_compaction fires (Felix R78: the last
     generation's usage.prompt_tokens above 50% of the model's context
     window, or the 2000-message backstop), the OLDEST non-system
     messages are summarized into the quadtree and the context trimmed
     ONLY on success. If the summary cannot be generated (issue #9), the
     messages STAY in the live context and compaction retries next turn;
     the material is also persisted verbatim to memory/qtree/raw/ first.
     Nothing silently disappears anymore.
  2. CONTEXT BLOCK — soul_block() appends the qtree view (every unrolled
     entry of every level — R75 lifetime coverage), so every generation
     carries gapless coverage of the whole compacted life.
  3. RESTORE — _sanitize tail-caps at 2500 messages (R78: above the
     2000-msg backstop, so the backstop fires BEFORE silent tail-cap
     loss); the qtree context block covers whatever fell outside that
     window long-term.

Summary generations go through the same proxy job queue (class "task" —
auxiliary, never agent-class billing), single-shot, tools disabled.
"""

from __future__ import annotations


# ---- 1. turn-end compaction -------------------------------------------------

async def maybe_compact(agent) -> dict:
    """Called in the turn's finally block. Returns stats (may be empty).
    Issue #9: the live context is trimmed ONLY after record_compaction
    succeeded — if summarization fails, the messages stay in the context
    (raw material is already persisted in memory/qtree/raw/) and the
    next turn retries compaction."""
    from . import qtree
    msgs = agent.messages
    pt = getattr(agent, "last_prompt_tokens", 0) or 0
    cw = getattr(agent, "context_window", 0) or 0
    if not qtree.needs_compaction(msgs, pt, cw):
        return {}
    non_sys = [(i, len(m.get("content") or ""))
               for i, m in enumerate(msgs) if m.get("role") != "system"]
    total_chars = sum(n for _, n in non_sys)
    drop_idx: set[int] = set()
    if pt and cw and pt > qtree.COMPACT_TRIGGER_WINDOW_FRACTION * cw:
        # R78 token path: drop OLDEST non-system messages until the
        # estimated remaining tokens land at <= 25% of the window. The
        # estimate is by char proportion — est = pt * kept/total — because
        # token density is roughly uniform and the exact provider-reported
        # total anchors it (est == pt when nothing is dropped).
        target = qtree.COMPACT_TARGET_FRACTION * cw
        acc_chars = 0
        for i, n in non_sys:               # oldest first
            if total_chars and pt * (total_chars - acc_chars) / total_chars \
                    <= target:
                break
            if len(non_sys) - len(drop_idx) <= 1:
                break                      # never drop the newest message
            drop_idx.add(i)
            acc_chars += n
    else:
        # Backstop path (no usage data yet / message count): the existing
        # halving — keep the newest COMPACT_TRIGGER_MSGS // 2 messages.
        budget_keep_msgs = qtree.COMPACT_TRIGGER_MSGS // 2
        acc_msgs = 0
        for i, n in non_sys:               # oldest first
            if len(non_sys) - acc_msgs <= budget_keep_msgs:
                break
            drop_idx.add(i)
            acc_msgs += 1
    if not drop_idx:
        return {}
    # Issue #12: the cut may land mid tool-exchange — extend the drop set
    # past tool results whose parent assistant(tool_calls) was dropped.
    # A kept leading 'tool' message 400s strict providers every turn.
    for i, m in enumerate(msgs):
        if i in drop_idx or m.get("role") == "system":
            continue
        if m.get("role") != "tool":
            break
        drop_idx.add(i)
    dropped = [m for i, m in enumerate(msgs) if i in drop_idx]
    kept = [m for i, m in enumerate(msgs) if i not in drop_idx]

    stats = {}
    try:
        stats = await qtree.record_compaction(
            agent.workspace, dropped, lambda p: _gen(agent, p),
            embed_fn=lambda texts: _embed(agent, texts))
    except Exception as e:  # noqa: BLE001 — never fail the turn for memory
        # issue #9: summarize failed — leave agent.messages UNTOUCHED so
        # nothing is dropped unrecoverably; the raw material is persisted
        # and compaction retries on the next turn.
        print(f"[qtree] compaction failed, context untouched: {e}")
        return stats
    # summarized OK — only now trim the live context
    agent.messages = kept
    n_kept = sum(1 for m in kept if m.get("role") != "system")
    # R78: log the numbers the decision was made on
    print(f"[qtree] compacted: prompt_tokens={pt} window={cw} "
          f"msgs={len(non_sys)} -> {n_kept} (dropped {len(drop_idx)})")
    try:
        # R70: the qtree view changed -> regenerate the system prompt so
        # the next generation sees the new entry
        agent._regen_system_prompt()
    except Exception as e:  # noqa: BLE001 — never fail the turn for memory
        print(f"[qtree] system-prompt regen failed: {e}")
    # R76: dreamer trigger check (cheap) after each compaction — the
    # dreamer itself never raises (it retries on the next compaction)
    try:
        from . import dreamer
        dstats = await dreamer.maybe_dream(agent)
        if dstats:
            stats["dream"] = dstats
    except Exception as e:  # noqa: BLE001 — never fail the turn for memory
        print(f"[dreamer] trigger check failed: {e}")
    return stats


async def _gen(agent, prompt: str, max_tokens: int = 2048) -> str:
    """Single-shot summary generation via the proxy (class "task")."""
    import asyncio
    from . import config
    settings = config.settings
    job = {"class_name": "task", "reason": "compaction",
           "messages": [{"role": "user", "content": prompt}],
           "tools": [], "max_tokens": max_tokens}
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


async def _embed(agent, texts: list[str]) -> list[list[float]]:
    """One embedding call via the proxy (designation "embedding", R76)."""
    from . import conclusions
    return await conclusions.embed_texts(agent.svcs, texts)


# ---- 2. context block in the system prompt ---------------------------------

def qtree_soul_extension(workspace: str) -> str:
    """Appended to <memory> in soul_block — all coarseness levels."""
    from . import qtree
    view = qtree.context_block(workspace)
    if not view:
        return ""
    return (f"<episodic-memory levels=\"{view.count(chr(10)) + 1}\">\n"
            f"{view}\n</episodic-memory>")
