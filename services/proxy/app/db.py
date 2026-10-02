import secrets

from motor.motor_asyncio import AsyncIOMotorClient

from .config import settings

client = AsyncIOMotorClient(settings.mongo_url)
db = client[settings.db_name]

agent_keys = db.agent_keys          # {_id: key, agent_id, created_at, revoked}
task_classes = db.task_classes      # {_id: name, provider_prefs: [...], default: bool}
providers = db.providers            # {_id: name, base_url, kind, rate_rpm, enabled}
model_catalog = db.model_catalog    # {_id: "provider/model", caps: [...], ctx, price_in, price_out, listed}
jobs = db.jobs                      # {agent_id, class, messages, tools, status, prio, ...}
usage_events = db.usage_events      # metering ledger (R32 reads this)
history = db.history                # full content per job (R34)

INDEXES = [
    (agent_keys, [("_id", 1)], True),
    (task_classes, [("_id", 1)], True),
    (providers, [("_id", 1)], True),
    (model_catalog, [("_id", 1)], True),
    (jobs, [("status", 1), ("prio", -1), ("created_at", 1)], False),
    (jobs, [("agent_id", 1), ("status", 1)], False),
    (usage_events, [("agent_id", 1), ("ts", -1)], False),
    (history, [("job_id", 1)], False),
]


async def ensure_indexes() -> None:
    for coll, keys, unique in INDEXES:
        if keys == [("_id", 1)]:
            continue  # _id index exists implicitly; unique not allowed on it
        await coll.create_index(keys, unique=unique)


def new_key() -> str:
    return "sk-agt-" + secrets.token_urlsafe(24)
