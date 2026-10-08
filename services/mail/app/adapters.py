"""Transport adapters. Outbound: sink (dev capture) | resend (API).
Inbound: resend (poll — implemented when a key exists; hook endpoint
provided now so simulated inbound can be E2E-tested on the dev box).

Adapters are selected by mail_adapters rows — data, not code (R38)."""
import json
import urllib.request
from datetime import datetime, timezone

import httpx

from .config import db, env_fallback, mail_messages, settings


def now():
    return datetime.now(timezone.utc)


# ---------------- outbound ----------------

async def send_sink(msg: dict) -> dict:
    """Dev capture: mark sent, store raw payload — nothing leaves the box."""
    return {"status": "sent", "via": "sink", "provider_id": None}


async def fetch_received_email(email_id: str) -> dict:
    """Resend received-emails API: GET /emails/{id}/received — returns
    {text, html, headers, ...}. Uses the same RESEND_API_KEY as outbound."""
    key = env_fallback("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set in environment")
    base = env_fallback("TG_RESEND_API_BASE") or "https://api.resend.com"
    req = urllib.request.Request(
        f"{base}/emails/{email_id}/received",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


async def send_resend(msg: dict) -> dict:
    """Resend REST: POST /emails. API key from env only."""
    key = env_fallback("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set in environment")
    payload = {
        "from": msg["from_addr"],
        "to": msg["to"] if isinstance(msg["to"], list) else [msg["to"]],
        "subject": msg["subject"],
    }
    if msg.get("text"):
        payload["text"] = msg["text"]
    if msg.get("html"):
        payload["html"] = msg["html"]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    return {"status": "sent", "via": "resend", "provider_id": out.get("id")}


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
    addresses filter syncs only those mailboxes. No webhook involved."""
    imported, skipped, failed = 0, 0, 0
    known = set()
    if addresses:
        for a in addresses:
            mb = await db.mailboxes.find_one({"address": a})
            if not mb:
                skipped += 1
                continue
    after = ""
    while True:
        page = list_received_emails(limit=100, after=after)
        rows = page.get("data") or []
        if not rows:
            break
        for m in rows:
            existing = await mail_messages.find_one({"resend_id": m["id"]})
            if existing:
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
            r = await ingest_inbound(to_addr, m.get("from") or "",
                                     m.get("subject") or "", text=text,
                                     wake=False, resend_id=m["id"],
                                     received_at=m.get("created_at"))
            if r.get("delivered"):
                imported += 1
            else:
                failed += 1
        if not page.get("has_more"):
            break
        after = rows[-1]["id"]
    return {"imported": imported, "skipped": skipped, "failed": failed}


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
            return {"delivered": False, "reason": "duplicate (already imported)"}
    mid = f"mail_{abs(hash((address, from_addr, subject, now().isoformat()))) % 10**16:016d}"
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
                "_id": f"wke_{abs(hash((mid, rid))) % 10**16:016d}",
                "agent_id": rid,
                "reason": "mail",
                "mail_message_id": mid,
                "created_at": now(),
                "consumed": False,
            })
            woke.append(rid)
    return {"delivered": True, "mailbox": address, "woke_agents": woke}
