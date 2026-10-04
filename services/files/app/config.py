import os

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = Field(default="mongodb://127.0.0.1:27017/?directConnection=true",
                       validation_alias=AliasChoices("TG_FILES_MONGO_URL", "TG_MONGO_URL"))
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8004
    service_token: str = "dev-service-token"

    # S3 host is USER-PROVIDED (R41): endpoint/bucket/keys from env, like Mongo.
    # endpoint scheme normalized: bare host gets https:// (Hetzner/AWS style)
    s3_endpoint: str = Field(default="", validation_alias=AliasChoices("TG_FILES_S3_ENDPOINT", "TG_S3_ENDPOINT"))
    s3_bucket: str = Field(default="treegent", validation_alias=AliasChoices("TG_FILES_S3_BUCKET", "TG_S3_BUCKET"))
    s3_region: str = Field(default="", validation_alias=AliasChoices("TG_FILES_S3_REGION", "TG_S3_REGION"))
    s3_access_key: str = Field(default="", validation_alias=AliasChoices("TG_FILES_S3_ACCESS_KEY"))
    s3_secret_key: str = Field(default="", validation_alias=AliasChoices("TG_FILES_S3_SECRET_KEY"))

    model_config = {"env_prefix": "TG_FILES_", "env_file": ".env", "extra": "ignore"}


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
