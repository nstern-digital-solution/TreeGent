from motor.motor_asyncio import AsyncIOMotorClient

from .config import settings

client = AsyncIOMotorClient(settings.mongo_url)
db = client[settings.db_name]

actors = db.actors
conversations = db.conversations
messages = db.messages
inbox = db.inbox
wake_events = db.wake_events
counters = db.counters
agent_keys = db.agent_keys
# mail-owned collections (shared treegent DB) — read/edited by the
# issue #20 cleanup paths (delete/reparent must not orphan approvals)
approvals = db.approvals
mailboxes = db.mailboxes
mail_messages = db.mail_messages

INDEXES = [
    (actors, [("username", 1)], True),
    (actors, [("org.ancestors", 1)], False),
    (conversations, [("members", 1)], False),
    (conversations, [("kind", 1), ("members", 1)], False),
    (messages, [("conversation_id", 1), ("seq", 1)], False),
    (inbox, [("recipient_id", 1), ("delivered_at", 1)], False),
    (inbox, [("message_id", 1), ("recipient_id", 1)], True),
    # R69: wake_events is polled by {agent_id, consumed} every 5s per
    # agent — compound index instead of the created_at-only scan
    (wake_events, [("agent_id", 1), ("consumed", 1), ("created_at", 1)],
     False),
]

# R69: TTL — consumed wakes and old usage/job bookkeeping expire instead
# of growing forever (7d retention; operators can archive before then)
TTL_INDEXES = [
    (wake_events, "created_at", 7 * 24 * 3600),
    (messages, "created_at", 90 * 24 * 3600),   # actual field name
]


async def ensure_indexes() -> None:
    for coll, keys, unique in INDEXES:
        await coll.create_index(keys, unique=unique)
    for coll, field, seconds in TTL_INDEXES:
        try:
            await coll.create_index(
                [(field, 1)], expireAfterSeconds=seconds,
                name=f"ttl_{field}")
        except Exception:  # noqa: BLE001 — existing index with other opts
            pass
