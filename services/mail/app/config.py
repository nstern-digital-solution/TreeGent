"""TreeGent mail service — personal + shared agent mailboxes, approval-gated
outbound, adapter-based transport (R24-R26, R35-R38).

Collections (shared treegent DB):
  mailboxes     {_id, address, kind: personal|shared, owner | members[]}
  mail_messages {_id, mailbox_id, direction: in|out, from, to, subject,
                 text, html, status: pending|sent|failed|received, ts,
                 approval_id?}
  approvals     {_id, action, payload, requester_id, approver_id,
                 status: pending|approved|rejected, created_at,
                 decided_at?, reason?}
  mail_adapters {_id, direction: outbound|inbound, kind: sink|resend,
                 enabled, config{}}

Transport is DATA: adapter rows choose sink (dev capture) or Resend
(RESEND_API_KEY in env). No vendor is hardcoded beyond adapter kinds."""
import os

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8002
    service_token: str = "dev-service-token"
    chat_url: str = "http://127.0.0.1:8000"
    mail_domain: str = "treegent.local"  # dev default; real = deployment data

    model_config = {"env_prefix": "TG_MAIL_", "env_file": ".env"}


def env_fallback(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    path = os.path.expanduser("~/.treegent/env")
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


settings = Settings()

client = AsyncIOMotorClient(settings.mongo_url)
db = client[settings.db_name]

mailboxes = db.mailboxes
mail_messages = db.mail_messages
approvals = db.approvals
mail_adapters = db.mail_adapters
actors = db.actors            # chat-owned; read for org/superior lookup
wake_events = db.wake_events  # chat-owned; mail inserts reason='mail' rows


async def ensure_indexes() -> None:
    await mailboxes.create_index("address", unique=True)
    await mailboxes.create_index("owner")
    await mail_messages.create_index([("mailbox_id", 1), ("ts", -1)])
    await approvals.create_index([("approver_id", 1), ("status", 1)])
    await approvals.create_index([("requester_id", 1), ("status", 1)])
