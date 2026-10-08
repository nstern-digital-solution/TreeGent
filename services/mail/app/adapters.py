"""Transport adapters. Outbound: sink (dev capture) | resend (API).
Inbound: resend (R67 pull — read-through sync from the receiving API;
hook endpoint provided so simulated inbound can be E2E-tested on the dev
box). No webhook exists (R67).

Adapters are selected by mail_adapters rows — data, not code (R38)."""
import asyncio
import json
import urllib.request
from datetime import datetime, timezone

import httpx

from .config import db, env_fallback, mail_messages, settings
from .idgen import new_id


def now():
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime:
    """Resend created_at (ISO 8601, e.g. 2026-10-01T08:00:00.000Z) ->
    aware datetime; now() on anything unparseable (R68: every stored row
    needs ts for the ts-sorted listings)."""
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return now()


# ---------------- outbound ----------------

async def send_sink(msg: dict) -> dict:
    """Dev capture: mark sent, store raw payload — nothing leaves the box."""
    return {"status": "sent", "via": "sink", "provider_id": None}


def _fetch_received_email_sync(email_id: str) -> dict:
    """Blocking urllib call — ONLY ever run via asyncio.to_thread (R68)."""
    key = env_fallback("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set in environment")
    base = env_fallback("TG_RESEND_API_BASE") or "https://api.resend.com"
    req = urllib.request.Request(
        f"{base}/emails/receiving/{email_id}",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


async def fetch_received_email(email_id: str) -> dict:
    """Resend receiving API: GET /emails/receiving/{id} (R68: the documented
    body path — /emails/{id}/received does not exist) — returns
    {text, html, headers, ...}. Uses the same RESEND_API_KEY as outbound."""
    return await asyncio.to_thread(_fetch_received_email_sync, email_id)


def _send_resend_sync(payload: dict) -> dict:
    """Blocking urllib call — ONLY ever run via asyncio.to_thread (R68)."""
    key = env_fallback("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set in environment")
    base = env_fallback("TG_RESEND_API_BASE") or "https://api.resend.com"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base}/emails", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    return {"status": "sent", "via": "resend", "provider_id": out.get("id")}


async def send_resend(msg: dict) -> dict:
    """Resend REST: POST /emails. API key from env only."""
    payload = {
        "from": msg["from_addr"],
        "to": msg["to"] if isinstance(msg["to"], list) else [msg["to"]],
        "subject": msg["subject"],
    }
    if msg.get("text"):
        payload["text"] = msg["text"]
    if msg.get("html"):
        payload["html"] = msg["html"]
    return await asyncio.to_thread(_send_resend_sync, payload)


OUTBOUND = {"sink": send_sink, "resend": send_resend}


async def dispatch_outbound(msg: dict) -> dict:
    """Send via the first enabled outbound adapter row."""
    row = await db.mail_adapters.find_one(
        {"direction": "outbound", "enabled": True})
    if not row:
        raise RuntimeError("no enabled outbound mail adapter configured")
    kind = row.get("kind", "sink")
    fn = OUTBOUND.get(kind)
    if not fn:
        raise RuntimeError(f"unknown outbound adapter kind {kind!r}")
    result = await fn(msg)
    await mail_messages.update_one(
        {"_id": msg["_id"]},
        {"$set": {"status": result["status"], "via": result["via"],
                  "provider_id": result.get("provider_id"),
                  "sent_at": now()}})
    return result


# ---------------- inbound ----------------

def list_received_emails(limit: int = 100, after: str = "") -> dict:
    """Resend receiving list: GET /emails/receiving — every inbound mail
    (id, to, from, subject, created_at), cursor-paginated."""
    key = env_fallback("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set in environment")
    base = env_fallback("TG_RESEND_API_BASE") or "https://api.resend.com"
    url = f"{base}/emails/receiving?limit={limit}"
    if after:
        url += f"&after={after}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


async def sync_inbound(addresses: list[str] | None = None) -> dict:
    """R67: pull inbound mail from Resend on demand (read-through). Called
    before serving a mailbox list/read. Imports anything new, deduped by
    Resend email id; never wakes agents (pull is not an event). Optional
    addresses filter syncs only those mailboxes. No webhook involved.
    R68: rows imported without a body (Resend body hiccup) get their body
    RETRIED on later syncs instead of being skipped forever by the dedupe."""
    imported, skipped, failed, backfilled = 0, 0, 0, 0
    if addresses:
        for a in addresses:
            mb = await db.mailboxes.find_one({"address": a})
            if not mb:
                skipped += 1
                continue
    after = ""
    while True:
        page = await asyncio.to_thread(list_received_emails, 100, after)
        rows = page.get("data") or []
        if not rows:
            break
        for m in rows:
            existing = await mail_messages.find_one({"resend_id": m["id"]})
            if existing and (existing.get("text") or "").strip():
                skipped += 1
                continue
            to_list = m.get("to") or []
            to_addr = (to_list[0] if to_list else "").strip().lower()
            if not to_addr or (addresses and to_addr not in addresses):
                skipped += 1
                continue
            text = ""
            try:
                fetched = await fetch_received_email(m["id"])
                text = fetched.get("text") or ""
            except Exception:  # noqa: BLE001 — metadata-only beats dropping
                pass
            if existing:  # bodyless row: backfill body (+ ts), never a dup
                update = {"text": text}
                if not existing.get("ts"):
                    update["ts"] = parse_ts(m.get("created_at"))
                await mail_messages.update_one({"_id": existing["_id"]},
                                               {"$set": update})
                backfilled += 1
                continue
            r = await ingest_inbound(to_addr, m.get("from") or "",
                                     m.get("subject") or "", text=text,
                                     wake=False, resend_id=m["id"],
                                     received_at=m.get("created_at"))
            if r.get("delivered"):
                imported += 1
                # R69 fix: pull-discovery wakes the owner once per NEW mail
                # (dedupe makes this idempotent). Without it nothing ever
                # signaled new inbound mail on any tier — has_work's
                # mail_pending count queried fields no writer ever wrote.
                mb = await db.mailboxes.find_one({"address": to_addr})
                if mb and mb.get("kind") == "personal":
                    await db.wake_events.update_one(
                        {"agent_id": mb["owner"], "reason": "mail",
                         "consumed": False},
                        {"$setOnInsert": {
                            "_id": f"wke_pull_{m['id'][:24]}",
                            "created_at": now(), "detail":
                                f"new mail from {m.get('from')}"}},
                        upsert=True)
            else:
                failed += 1
        if not page.get("has_more"):
            break
        after = rows[-1]["id"]
    return {"imported": imported, "skipped": skipped, "failed": failed,
            "backfilled": backfilled}


async def ingest_inbound(address: str, from_addr: str, subject: str,
                         text: str = "", html: str = "",
                         wake: bool = True, resend_id: str = "",
                         received_at: str = "") -> dict:
    """Route one inbound email into its mailbox. Pull-sync (R67) or the dev
    simulate hook. Shared mailbox used to wake every member (R35) — wakes
    only happen for pushed mail, which no longer exists in prod (R67);
    the dev hook still wakes."""
    mb = await db.mailboxes.find_one({"address": address})
    if not mb:
        return {"delivered": False, "reason": f"no mailbox {address!r}"}
    if resend_id:
        dup = await mail_messages.find_one({"resend_id": resend_id})
        if dup:
            # R68: dedupe skips ONLY rows that already carry a body — a
            # bodyless row gets the body backfilled in place, never a dup.
            if (dup.get("text") or "").strip() or not (text or "").strip():
                return {"delivered": False,
                        "reason": "duplicate (already imported)"}
            await mail_messages.update_one({"_id": dup["_id"]},
                                           {"$set": {"text": text}})
            return {"delivered": True, "mailbox": address, "backfilled": True,
                    "woke_agents": []}
    mid = new_id("mail")
    msg = {
        "_id": mid,
        "mailbox_id": mb["_id"],
        "direction": "in",
        "from_addr": from_addr,
        "to": address,
        "subject": subject,
        "text": text,
        "html": html or None,
        "status": "received",
        "received_at": (received_at or now().isoformat()),
        # R68: ts (datetime, sorted by listings) alongside the raw
        # received_at string — imported mail used to have no ts at all
        "ts": parse_ts(received_at) if received_at else now(),
        **({"resend_id": resend_id} if resend_id else {}),
    }
    await mail_messages.insert_one(msg)
    if not wake:
        return {"delivered": True, "mailbox": address, "woke_agents": []}
    recipients = ([mb["owner"]] if mb["kind"] == "personal"
                  else mb.get("members", []))
    woke = []
    for rid in recipients:
        actor = await db.actors.find_one({"_id": rid})
        if actor and actor.get("kind") == "agent":
            await db.wake_events.insert_one({
                "_id": new_id("wke"),
                "agent_id": rid,
                "reason": "mail",
                "mail_message_id": mid,
                "created_at": now(),
                "consumed": False,
            })
            woke.append(rid)
    return {"delivered": True, "mailbox": address, "woke_agents": woke}
