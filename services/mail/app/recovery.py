"""Crash-window recovery for the approval state machine (issue #13).

decide() claims an approval pending -> dispatching and only a happy-path
completion moves it out — any kill/restart/deploy mid-request stranded it in
'dispatching' FOREVER (decide 409s on status != pending, no timer ever
touched it again). Mirrors the proven proxy R68 pattern (STALE_DISPATCHED_S
+ requeue at startup + periodic sweep, services/proxy/app/dispatcher.py).

Three crash windows, three heals:
1. claim .. provider-accept: stale 'dispatching' rows are requeued to
   'pending' after STALE_DISPATCHING_S and can be re-decided.
2. provider-accept .. row update: the requeue would RE-EXPOSE a double send,
   so dispatch_outbound is idempotent — provider Idempotency-Key = mail id
   plus a sent-row replay guard (adapters.py).
3. approved/rejected write .. wake insert: the requester wake uses the
   deterministic id wke_appr_<approval_id> with $setOnInsert, and this sweep
   re-inserts any wake lost to a crash exactly once (never a duplicate).
"""
import asyncio
from datetime import datetime, timedelta, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


# A legitimate dispatch is ONE provider call (30s urllib timeout) inside the
# decide request — nothing legitimate stays 'dispatching' past ~5 min.
STALE_DISPATCHING_S = 300
# Wake healing is bounded: a lost notification older than this is stale news
# — and pre-fix approvals must not all wake their requesters at once.
WAKE_HEAL_WINDOW_S = 3600


def stale_cutoff() -> datetime:
    return now() - timedelta(seconds=STALE_DISPATCHING_S)


def wake_heal_cutoff() -> datetime:
    return now() - timedelta(seconds=WAKE_HEAL_WINDOW_S)


def appr_wake_id(approval_id: str) -> str:
    """Deterministic requester-wake id (issue #13): one per approval, the
    same on every retry — combined with $setOnInsert the insert is
    idempotent, so crash retries and the recovery sweep cannot duplicate."""
    return f"wke_appr_{approval_id}"


async def wake_requester(wake_events, approval_id: str, requester_id: str,
                         detail: str, reason: str = "approval") -> bool:
    """Insert the requester wake idempotently. Returns True when a wake was
    actually inserted (False = one already existed)."""
    res = await wake_events.update_one(
        {"_id": appr_wake_id(approval_id)},
        {"$setOnInsert": {
            "agent_id": requester_id, "reason": reason,
            "approval_id": approval_id, "detail": detail,
            "created_at": now(), "consumed": False}},
        upsert=True)
    return bool(getattr(res, "upserted_id", None))


async def requeue_stale(approvals, reason: str = "startup") -> int:
    """Requeue approvals stranded in 'dispatching' back to 'pending'
    (decided_at is the claim timestamp, cleared on requeue; requeues keeps
    the history instead of wiping it — proxy R68 shape). A later decide
    re-runs the send; dispatch_outbound's idempotency keeps that retry from
    double-sending even when the provider already accepted the mail."""
    res = await approvals.update_many(
        {"status": "dispatching", "decided_at": {"$lt": stale_cutoff()}},
        {"$set": {"status": "pending", "decided_at": None,
                  "detail": "requeued from dispatching (stale crash window)"},
         "$inc": {"requeues": 1}})
    n = getattr(res, "modified_count", 0)
    if n:
        print(f"[mail] requeued {n} stale dispatching approval(s) ({reason})",
              flush=True)
    return n


async def heal_lost_wakes(approvals, wake_events,
                          reason: str = "startup") -> int:
    """Re-create requester wakes lost in the decided -> wake insert crash
    window. Any wake already referencing the approval (including pre-fix
    random-id rows) counts as delivered — never a duplicate."""
    n = 0
    async for a in approvals.find(
            {"status": {"$in": ["approved", "rejected"]},
             "decided_at": {"$gte": wake_heal_cutoff()}}):
        if await wake_events.find_one({"approval_id": a["_id"]}):
            continue
        if a["status"] == "rejected":
            r = "approval-rejected"
            detail = (a.get("reason") or "")[:500]
        else:
            r = "approval"
            detail = (f"approved — mail sent to "
                      f"{(a.get('payload') or {}).get('to')}")
        if await wake_requester(wake_events, a["_id"], a["requester_id"],
                                detail, reason=r):
            n += 1
    if n:
        print(f"[mail] healed {n} lost requester wake(s) ({reason})", flush=True)
    return n


async def recovery_loop(approvals, wake_events,
                        interval_s: float = 30.0) -> None:
    """Periodic sweep (mirrors the proxy dispatcher's 30s idle sweep) so the
    crash windows heal without a restart."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await requeue_stale(approvals, "sweep")
            await heal_lost_wakes(approvals, wake_events, "sweep")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a sweep failure must not kill it
            print(f"[mail] recovery sweep failed: {type(e).__name__}: "
                  f"{str(e)[:150]}", flush=True)
