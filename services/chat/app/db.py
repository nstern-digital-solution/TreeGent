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

INDEXES = [
    (actors, [("username", 1)], True),
    (actors, [("org.ancestors", 1)], False),
    (conversations, [("members", 1)], False),
    (conversations, [("kind", 1), ("members", 1)], False),
    (messages, [("conversation_id", 1), ("seq", 1)], False),
    (inbox, [("recipient_id", 1), ("delivered_at", 1)], False),
    (inbox, [("message_id", 1), ("recipient_id", 1)], True),
    (wake_events, [("created_at", 1)], False),
]


async def ensure_indexes() -> None:
    for coll, keys, unique in INDEXES:
        await coll.create_index(keys, unique=unique)
