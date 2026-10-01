from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = "mongodb://127.0.0.1:27017/?directConnection=true"
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    # shared secret for trusted callers (Meteor server). Real per-actor tokens
    # arrive with services/control (R22); M1 uses the trusted-service header.
    service_token: str = "dev-service-token"

    model_config = {"env_prefix": "TG_CHAT_", "env_file": ".env"}


settings = Settings()
