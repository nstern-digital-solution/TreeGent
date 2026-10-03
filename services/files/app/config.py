import os

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8004
    service_token: str = "dev-service-token"

    # S3 host is USER-PROVIDED (R41): endpoint/bucket/keys from env, like Mongo.
    s3_endpoint: str = ""            # empty = default AWS; or https://host:port
    s3_bucket: str = "treegent"
    s3_region: str = ""
    s3_access_key_env: str = "TG_S3_ACCESS_KEY"   # env var NAMES, not values
    s3_secret_key_env: str = "TG_S3_SECRET_KEY"

    model_config = {"env_prefix": "TG_FILES_", "env_file": ".env"}


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

files_col = db.files   # {_id, name, size, content_type, owner, shared_with,
                       #  s3_key, created_at, updated_at}


async def ensure_indexes() -> None:
    await files_col.create_index([("owner", 1), ("updated_at", -1)])
    await files_col.create_index("shared_with")
