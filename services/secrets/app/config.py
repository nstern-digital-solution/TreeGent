import os

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8003
    service_token: str = "dev-service-token"
    # master key file for value encryption (instance data, never in Mongo/repo)
    key_file: str = os.path.expanduser("~/.treegent/secrets.key")

    model_config = {"env_prefix": "TG_SECRETS_", "env_file": ".env"}


settings = Settings()

client = AsyncIOMotorClient(settings.mongo_url)
db = client[settings.db_name]

secrets_col = db.secrets   # {_id, name, username, url, notes, value_enc,
                           #  owner, shared_with, created_at, updated_at}


async def ensure_indexes() -> None:
    await secrets_col.create_index([("owner", 1), ("updated_at", -1)])
    await secrets_col.create_index("shared_with")
