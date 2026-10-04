import os

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = Field(default="mongodb://127.0.0.1:27017/?directConnection=true",
                       validation_alias=AliasChoices("TG_PROXY_MONGO_URL", "TG_MONGO_URL"))
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8001
    service_token: str = Field(default="dev-service-token",
                            validation_alias=AliasChoices("TG_PROXY_SERVICE_TOKEN", "TG_SERVICE_TOKEN"))
    chat_url: str = "http://127.0.0.1:8000"

    model_config = {"env_prefix": "TG_PROXY_", "env_file": ".env", "extra": "ignore"}


def env_fallback(name: str) -> str:
    """Read a var from the environment, falling back to the runtime env
    file (~/.treegent/env). Provider keys live ONLY here — never in Mongo."""
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
