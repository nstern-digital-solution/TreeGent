import os

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = Field(default="mongodb://127.0.0.1:27017/?directConnection=true",
                       validation_alias=AliasChoices("TG_RUNTIME_MONGO_URL", "TG_MONGO_URL"))
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8010
    service_token: str = Field(default="dev-service-token",
                            validation_alias=AliasChoices("TG_RUNTIME_SERVICE_TOKEN", "TG_SERVICE_TOKEN"))
    # shared names (TG_CHAT_URL...) as written by provisioning; the
    # TG_RUNTIME_-prefixed forms keep working too
    chat_url: str = Field(default="http://127.0.0.1:8000",
                      validation_alias=AliasChoices("TG_RUNTIME_CHAT_URL", "TG_CHAT_URL"))
    proxy_url: str = Field(default="http://127.0.0.1:8001",
                      validation_alias=AliasChoices("TG_RUNTIME_PROXY_URL", "TG_PROXY_URL"))
    mail_url: str = Field(default="http://127.0.0.1:8002",
                      validation_alias=AliasChoices("TG_RUNTIME_MAIL_URL", "TG_MAIL_URL"))
    secrets_url: str = Field(default="http://127.0.0.1:8003",
                      validation_alias=AliasChoices("TG_RUNTIME_SECRETS_URL", "TG_SECRETS_URL"))
    files_url: str = Field(default="http://127.0.0.1:8004",
                      validation_alias=AliasChoices("TG_RUNTIME_FILES_URL", "TG_FILES_URL"))
    # agent workspaces root (the mounted share lives under here)
    workspace_root: str = os.path.expanduser("~/treegent-workspaces")
    heartbeat_s: int = 3600            # R44: default 60 min, per-agent override
    exec_timeout_s: int = 600          # R46: foreground cap 10 min
    exec_user: str = ""                 # R46: separate linux user; sudo -n -u
    exec_enabled: bool = True            # R48: False on reserved hosts
    host_id: str = Field(default="",
                      validation_alias=AliasChoices("TG_RUNTIME_HOST_ID", "TG_HOST_ID"))
    # R56: per-host secret from provisioning; presence = hosted (no-Mongo) mode
    host_key: str = Field(default="",
                      validation_alias=AliasChoices("TG_RUNTIME_HOST_KEY", "TG_HOST_KEY"))
    # (TG_RUNTIME_EXEC_ENABLED shared name handled by prefix already)
    max_turn_steps: int = 24           # tool-call steps per turn before forced stop
    search_backend: str = ""           # "" = web search disabled (R42)

    model_config = {"env_prefix": "TG_RUNTIME_", "env_file": ".env", "extra": "ignore"}


settings = Settings()

# R56: hosted runtimes (host_key set) NEVER touch Mongo — no credential on
# the box. The client handle exists only for the central runtime path.
if settings.host_key:
    client = None  # type: ignore[assignment]
    db = None      # type: ignore[assignment]
else:
    client = AsyncIOMotorClient(settings.mongo_url)
    db = client[settings.db_name]

# runtime-owned collections (central runtime only; hosted mode has no db)
turns = db.runtime_turns if db is not None else None
memories = db.agent_memory if db is not None else None
