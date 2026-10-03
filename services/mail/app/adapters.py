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

async def ingest_inbound(address: str, from_addr: str, subject: str,
                         text: str = "", html: str = "") -> dict:
    """Route one inbound email into its mailbox. Called by the inbound
    adapter (webhook/poll) or the dev simulate hook. Shared mailbox →
    wakes every member agent; personal → wakes the owner agent (R35)."""
    mb = await db.mailboxes.find_one({"address": address})
    if not mb:
        return {"delivered": False, "reason": f"no mailbox {address!r}"}
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
        "received_at": now(),
    }
    await mail_messages.insert_one(msg)
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
