from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8001
    service_token: str = "dev-service-token"
    chat_url: str = "http://127.0.0.1:8000"
    # provider keys are read from the environment (never stored in Mongo)
    openrouter_api_key: str = ""

    model_config = {"env_prefix": "TG_PROXY_", "env_file": ".env"}


def _hermes_env_fallback(name: str) -> str:
    """Read a var from ~/.treegent/env if not set in the environment."""
    import os
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
if not settings.openrouter_api_key:
    settings.openrouter_api_key = _hermes_env_fallback("OPENROUTER_API_KEY")
