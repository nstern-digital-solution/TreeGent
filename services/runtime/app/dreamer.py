"""Per-agent dreamer (R76): periodic maintenance of the derived conclusions.

Trigger check runs after each compaction (cheap — file reads only):

  run when BOTH
    (a) new_conclusions_since_last_dream >= DREAMER_FACTOR *
        max(1, conclusions_at_last), AND
    (b) now - last_dream >= DREAMER_MIN_S
  OR unconditionally when now - last_dream >= DREAMER_MAX_S.
  First-ever run when >= DREAMER_FIRST_RUN conclusions exist.

The dream itself is ONE task-class generation (proxy, tools off) fed the
conclusions since the last dream (capped DREAMER_FEED_CAP), asked for a
JSON plan: dedupes (ids to supersede + replacement text), merges,
contradictions (keep newest, supersede older), and cross-cutting patterns
as NEW conclusions of kind "dreamer". Applied atomically-ish (append new +
patch superseded_by via a rewritten temp file + os.replace), then
dreamer.json advances. A failed dream leaves the state untouched and
retries on the next compaction — the whole module is wrapped so it can
NEVER break a turn.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import conclusions as C

DREAMER_FACTOR = 2
DREAMER_MIN_S = 6 * 3600            # amount trigger never fires sooner
DREAMER_MAX_S = 14 * 86400          # wall-clock backstop, unconditional
DREAMER_FIRST_RUN = 20              # first-ever dream gate
DREAMER_FEED_CAP = 200              # conclusions fed per dream
DREAM_MAX_TOKENS = 4096

_DREAM_PROMPT = """You maintain the derived long-term memory of an \
autonomous agent. Below are conclusions derived from its compacted \
history since the last maintenance pass, one per line:
<id> | <kind> | <era> | <text> | premises: <refs>

Return ONLY a JSON object:
{{"dedupes": [{{"ids": ["cnc_..."], "text": "replacement conclusion"}}],
  "merges": [{{"ids": ["cnc_...", "cnc_..."], "text": "one merged conclusion"}}],
  "contradictions": [{{"ids": ["cnc_...", "cnc_..."]}}],
  "patterns": ["cross-cutting pattern worth remembering"]}}

Rules:
- dedupes: several conclusions say the same thing -> supersede them all \
with ONE new replacement text
- merges: related conclusions better kept as one -> supersede them with \
ONE merged text
- contradictions: conclusions that conflict -> list their ids; the NEWEST \
is kept and the rest are superseded
- patterns: NEW cross-cutting conclusions to add
- every text max 400 characters; empty lists are fine; omit sections you \
do not need; no prose outside the JSON

Conclusions:
{feed}"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------- trigger ----------------

def dreamer_due(state: dict, total: int, now: datetime) -> bool:
    """The cadence rule (operator-ruled 2026-10-11): amount factor AND
    wall-clock min/max. First-ever run at >= DREAMER_FIRST_RUN."""
    last = _parse_ts(state.get("last_dream"))
    at_last = int(state.get("conclusions_at_last") or 0)
    if last is None:
        return total >= DREAMER_FIRST_RUN
    elapsed = (now - last).total_seconds()
    if elapsed >= DREAMER_MAX_S:
        return True
    new = total - at_last
    return (new >= DREAMER_FACTOR * max(1, at_last)
            and elapsed >= DREAMER_MIN_S)


def _feed(entries: list[dict], since: int) -> list[dict]:
    """Conclusions appended since the last dream (positional — the store is
    append-only), already-superseded rows skipped, newest cap last."""
    fresh = [e for e in entries[max(0, since):] if not e.get("superseded_by")]
    return fresh[-DREAMER_FEED_CAP:]


def dream_prompt(feed: list[dict]) -> str:
    lines = []
    for e in feed:
        premises = ", ".join(str(p) for p in (e.get("premises") or []))
        lines.append(f"{e.get('id')} | {e.get('kind', 'task')} | "
                     f"{e.get('era', '?')} | {e.get('text', '')} | "
                     f"premises: {premises}")
    return _DREAM_PROMPT.format(feed="\n".join(lines))


# ---------------- plan parsing ----------------

def parse_dream_output(text: str) -> dict:
    """Extract the JSON plan (tolerant of code fences / prose). RAISES on
    unparseable output — the dream then fails and retries next compaction."""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise RuntimeError("dream returned no JSON object")
    plan = json.loads(m.group(0))
    if not isinstance(plan, dict):
        raise RuntimeError("dream plan is not an object")
    out = {"dedupes": [], "merges": [], "contradictions": [], "patterns": []}
    for key in out:
        vals = plan.get(key)
        if isinstance(vals, list):
            out[key] = vals
    return out


# ---------------- apply ----------------

def _valid_ids(ids, by_id: dict, supersede: dict) -> list[str]:
    good = []
    for i in ids or []:
        i = str(i)
        if i in by_id and i not in supersede and not by_id[i].get("superseded_by"):
            good.append(i)
    return good


async def run_dream(workspace: str, gen_fn, embed_fn=None,
                    now: datetime | None = None) -> dict:
    """One dream cycle — performs the dream unconditionally (the cadence
    gate lives in maybe_dream, the trigger). RAISES on failure — state
    only advances at the very end, so a failed dream retries on the next
    compaction."""
    now = now or _now()
    state = C.read_dreamer_state(workspace)
    entries = C.load_conclusions(workspace)
    total = len(entries)
    feed = _feed(entries, int(state.get("conclusions_at_last") or 0))
    if not feed:
        # nothing new to dream about — just advance the clock
        C.write_dreamer_state(workspace, {
            "last_dream": now.isoformat(), "conclusions_at_last": total})
        return {"skipped": "no new conclusions"}
    plan = parse_dream_output(await gen_fn(dream_prompt(feed)))

    by_id = {e["id"]: e for e in entries}
    supersede: dict[str, str] = {}
    new_recs: list[dict] = []
    newest_era = feed[-1].get("era", "L0:0")

    def _replacement(text: str, premises: list[str]) -> dict:
        return C.make_record(text=text, kind="dreamer", era=newest_era,
                             premises=premises, ts=now.isoformat())

    # dedupes + merges: supersede all listed -> ONE replacement
    for group in (plan["dedupes"] or []) + (plan["merges"] or []):
        if not isinstance(group, dict):
            continue
        ids = _valid_ids(group.get("ids"), by_id, supersede)
        if not ids:
            continue
        repl = _replacement(str(group.get("text") or ""), ids)
        new_recs.append(repl)
        for i in ids:
            supersede[i] = repl["id"]

    # contradictions: keep the newest, supersede the older
    for group in plan["contradictions"]:
        if not isinstance(group, dict):
            continue
        ids = _valid_ids(group.get("ids"), by_id, supersede)
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: str(by_id[i].get("ts") or ""), reverse=True)
        keep = ids[0]
        for i in ids[1:]:
            supersede[i] = keep

    # patterns: NEW dreamer conclusions
    for pat in plan["patterns"]:
        if isinstance(pat, str) and pat.strip():
            new_recs.append(_replacement(pat.strip(), []))

    # embed the new texts (batched) — degrade to no-vectors on failure
    if new_recs and embed_fn is not None:
        try:
            vecs = await embed_fn([r["text"] for r in new_recs])
            if len(vecs) == len(new_recs):
                for r, v in zip(new_recs, vecs):
                    r["embedding"] = list(v)
        except Exception as e:  # noqa: BLE001 — degraded, never broken
            print(f"[dreamer] embedding failed ({e}); storing unembedded")

    C.apply_dream(workspace, supersede, new_recs)
    C.write_dreamer_state(workspace, {
        "last_dream": now.isoformat(),
        "conclusions_at_last": total + len(new_recs)})
    return {"new": len(new_recs), "superseded": len(supersede)}


async def maybe_dream(agent, gen_fn=None, embed_fn=None,
                      now: datetime | None = None) -> dict:
    """Cheap trigger check after each compaction; runs the dream when due.
    NEVER raises — a failed dream logs and retries on the next compaction
    (state not advanced)."""
    try:
        ws = agent.workspace
        if gen_fn is None:
            from . import compact
            gen_fn = lambda p: compact._gen(  # noqa: E731
                agent, p, max_tokens=DREAM_MAX_TOKENS)
        if embed_fn is None:
            embed_fn = lambda texts: C.embed_texts(  # noqa: E731
                agent.svcs, texts)
        state = C.read_dreamer_state(ws)
        total = C.count_conclusions(ws)
        if not dreamer_due(state, total, now or _now()):
            return {}
        return await run_dream(ws, gen_fn, embed_fn, now=now)
    except Exception as e:  # noqa: BLE001 — memory must never break the loop
        print(f"[dreamer] dream failed (retries next compaction): {e}")
        return {}
