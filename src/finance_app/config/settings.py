from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables / .env.

    Never gains a default that points at a production value — see
    CLAUDE.md and docs/security-model.md. Values are read from the
    environment; this repository never contains a real credential.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    finance_env: str = "development"

    database_url: str = "postgresql+psycopg://finance_app:devpassword@localhost:5433/finance_dev"

    plaid_env: str = "sandbox"
    plaid_client_id: str = ""
    plaid_secret: SecretStr = SecretStr("")
    plaid_access_token: SecretStr = SecretStr("")
    plaid_webhook_secret: SecretStr = SecretStr("")

    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""

    log_level: str = "INFO"
    log_format: str = "json"


def get_settings() -> Settings:
    return Settings()
