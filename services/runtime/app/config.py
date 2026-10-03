import os

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8010
    service_token: str = "dev-service-token"
    chat_url: str = "http://127.0.0.1:8000"
    proxy_url: str = "http://127.0.0.1:8001"
    mail_url: str = "http://127.0.0.1:8002"
    secrets_url: str = "http://127.0.0.1:8003"
    files_url: str = "http://127.0.0.1:8004"
    # agent workspaces root (the mounted share lives under here)
    workspace_root: str = os.path.expanduser("~/treegent-workspaces")
    heartbeat_s: int = 3600            # R44: default 60 min, per-agent override
    exec_timeout_s: int = 120
    max_turn_steps: int = 24           # tool-call steps per turn before forced stop
    search_backend: str = ""           # "" = web search disabled (R42)

    model_config = {"env_prefix": "TG_RUNTIME_", "env_file": ".env"}


settings = Settings()

client = AsyncIOMotorClient(settings.mongo_url)
db = client[settings.db_name]

# runtime-owned collections
turns = db.runtime_turns       # one doc per agent turn (audit of the loop)
memories = db.agent_memory     # {agent_id, text, ts} — memory search corpus
