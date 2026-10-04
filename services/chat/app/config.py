from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_url: str = Field(default="mongodb://127.0.0.1:27017/?directConnection=true",
                       validation_alias=AliasChoices("TG_CHAT_MONGO_URL", "TG_MONGO_URL"))
    db_name: str = "treegent"
    bind_host: str = "127.0.0.1"
    port: int = 8000
    # shared secret for trusted callers (Meteor server). Real per-actor tokens
    # arrive with services/control (R22); M1 uses the trusted-service header.
    service_token: str = Field(default="dev-service-token",
                            validation_alias=AliasChoices("TG_CHAT_SERVICE_TOKEN", "TG_SERVICE_TOKEN"))

    model_config = {"env_prefix": "TG_CHAT_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
