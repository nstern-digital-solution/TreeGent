import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8001
    service_token: str = "dev-service-token"
    chat_url: str = "http://127.0.0.1:8000"

    model_config = {"env_prefix": "TG_PROXY_", "env_file": ".env"}


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
