"""Derived-conclusions store (R76 — Honcho-inspired derived memory).

One JSON object per line in `memory/conclusions.jsonl`, APPEND-ONLY — the
same philosophy as the qtree L-files: lines are never deleted, the dreamer
marks `superseded_by` instead. Schema per record:

  {"id": "cnc_<rand>", "text": "...", "premises": ["L0:21" | "raw/<file>"],
   "kind": "peer:<username>" | "task" | "state" | "dreamer",
   "era": "L0:21", "ts": iso, "embedding": [floats],   # omitted when
                                                        # unavailable
   "superseded_by": null | "cnc_..."}

`memory/dreamer.json`: {"last_dream": iso|null, "conclusions_at_last": int}

Storage is workspace FILES, not Mongo (R56: hosted runtimes have NO Mongo).
Similarity search is brute-force cosine IN-PROCESS over the embeddings in
the JSONL — $vectorSearch is Atlas-only and NOT available on the
self-managed mongod. Documented ceiling: fine to ~50k conclusions per
agent (COSINE_CEILING); beyond that a real ANN index becomes worth adding.

Conclusions are DATA (like qtree entries): they are derived from untrusted
conversational material and are always presented to the model as quoted
memory lines, never as instructions.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

MEM_DIR = "memory"
CONCLUSIONS_NAME = "conclusions.jsonl"
DREAMER_NAME = "dreamer.json"
CONCLUSION_TEXT_MAX = 400
COSINE_CEILING = 50_000   # brute-force in-process cosine stays fine to ~50k


def _paths(workspace: str) -> dict:
    base = os.path.join(workspace, MEM_DIR)
    return {"base": base,
            "conclusions": os.path.join(base, CONCLUSIONS_NAME),
            "dreamer": os.path.join(base, DREAMER_NAME)}


def new_conclusion_id() -> str:
    return "cnc_" + secrets.token_hex(8)


def make_record(text: str, kind: str, era: str, premises: list[str],
                ts: str | None = None, embedding: list[float] | None = None,
                rec_id: str | None = None,
                superseded_by: str | None = None) -> dict:
    """One store record. The `embedding` field is OMITTED entirely when no
    vector is available (degraded mode — search skips such rows)."""
    rec = {"id": rec_id or new_conclusion_id(),
           "text": (text or "")[:CONCLUSION_TEXT_MAX],
           "premises": list(premises or []),
           "kind": kind, "era": era,
           "ts": ts or datetime.now(timezone.utc).isoformat(),
           "superseded_by": superseded_by}
    if embedding is not None:
        rec["embedding"] = list(embedding)
    return rec


# ---------------- storage I/O (append-only) ----------------

def load_conclusions(workspace: str) -> list[dict]:
    """Parsed records in file (= append) order. Malformed lines are
    skipped, never fatal — search/memory must never error."""
    out: list[dict] = []
    try:
        with open(_paths(workspace)["conclusions"], encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    out.append(rec)
    except OSError:
        pass
    return out


def count_conclusions(workspace: str) -> int:
    return len(load_conclusions(workspace))


def append_records(workspace: str, records: list[dict]) -> None:
    """Append records as fresh lines (no dedupe, no deletes)."""
    if not records:
        return
    p = _paths(workspace)
    os.makedirs(p["base"], exist_ok=True)
    with open(p["conclusions"], "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False,
                               sort_keys=True) + "\n")


def apply_dream(workspace: str, supersede: dict[str, str],
                new_records: list[dict]) -> None:
    """Dreamer apply — atomic-ish: every original line survives (patched
    `superseded_by`, never deleted) and the new records ride at the end,
    written to a temp file and swapped in with os.replace (crash-safe).
    `supersede` maps source id -> the id superseding it (replacement
    conclusion id, or the KEPT newest id for a contradiction)."""
    p = _paths(workspace)
    tmp = p["conclusions"] + ".tmp"
    try:
        with open(p["conclusions"], encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    with open(tmp, "w", encoding="utf-8") as f:
        for raw in lines:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                f.write(raw if raw.endswith("\n") else raw + "\n")
                continue
            if isinstance(rec, dict) and rec.get("id") in supersede:
                rec["superseded_by"] = supersede[rec["id"]]
                f.write(json.dumps(rec, ensure_ascii=False,
                                   sort_keys=True) + "\n")
            else:
                f.write(raw if raw.endswith("\n") else raw + "\n")
        for rec in new_records:
            f.write(json.dumps(rec, ensure_ascii=False,
                               sort_keys=True) + "\n")
    os.replace(tmp, p["conclusions"])


# ---------------- dreamer state ----------------

def read_dreamer_state(workspace: str) -> dict:
    """{"last_dream": iso|null, "conclusions_at_last": int}"""
    try:
        with open(_paths(workspace)["dreamer"]) as f:
            st = json.load(f)
        return {"last_dream": st.get("last_dream"),
                "conclusions_at_last": int(st.get("conclusions_at_last") or 0)}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"last_dream": None, "conclusions_at_last": 0}


def write_dreamer_state(workspace: str, state: dict) -> None:
    p = _paths(workspace)
    os.makedirs(p["base"], exist_ok=True)
    with open(p["dreamer"], "w") as f:
        json.dump({"last_dream": state.get("last_dream"),
                   "conclusions_at_last":
                       int(state.get("conclusions_at_last") or 0)},
                  f, indent=1)


# ---------------- embeddings via the proxy ----------------

async def embed_texts(svcs, texts: list[str],
                      timeout: float = 10.0) -> list[list[float]]:
    """One POST /v1/embeddings call (designation "embedding") through the
    metered proxy; returns vectors in input order. RAISES on failure —
    callers degrade to storing/skipping without vectors."""
    from .config import settings
    r = await svcs._call(settings.proxy_url, "/v1/embeddings", "POST",
                         body={"input": list(texts),
                               "designation": "embedding"},
                         timeout=timeout)
    rows = sorted(r.get("data") or [], key=lambda d: d.get("index", 0))
    vecs = [d.get("embedding") for d in rows]
    if len(vecs) != len(texts) or any(not isinstance(v, list) for v in vecs):
        raise RuntimeError("embedding response shape mismatch")
    return vecs


# ---------------- in-process cosine ranking ----------------

def cosine(a: list[float], b: list[float]) -> float:
    num = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        num += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return num / ((na ** 0.5) * (nb ** 0.5))


def search_conclusions(workspace: str, qvec: list[float],
                       k: int = 5) -> list[dict]:
    """Top-k non-superseded conclusions by cosine similarity. Rows without
    an embedding (degraded writes) are SKIPPED, never an error; k clamps
    to 1..20. Brute-force over the JSONL — see COSINE_CEILING."""
    k = max(1, min(int(k), 20))
    cands = [e for e in load_conclusions(workspace)
             if e.get("embedding") and not e.get("superseded_by")]
    scored = sorted(cands, key=lambda e: -cosine(qvec, e["embedding"]))
    return scored[:k]


def format_conclusion(rec: dict) -> str:
    """`  [peer:alice, era L0:21] <text> (premises: ...)`"""
    line = f"  [{rec.get('kind', 'task')}, era {rec.get('era', '?')}] " \
           f"{rec.get('text', '')}"
    premises = rec.get("premises") or []
    if premises:
        line += f" (premises: {', '.join(str(p) for p in premises)})"
    return line
