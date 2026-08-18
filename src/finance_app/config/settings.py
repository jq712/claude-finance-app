from typing import Literal

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
    # Migrations run DDL and create roles, so they use the bootstrap/owner
    # role rather than the least-privilege `finance_app` runtime role.
    alembic_database_url: str = (
        "postgresql+psycopg://finance_migrator:devpassword@localhost:5433/finance_dev"
    )
    # The runtime agent's own least-privilege connection (handoff §7.1,
    # ADR-014). Deliberately separate from `database_url` (`finance_app`,
    # which can write plaid.*) — using the wrong connection here would
    # silently defeat the database-level boundary that keeps the agent off
    # raw Plaid facts. See docs/security-model.md invariant 5.
    agent_database_url: str = (
        "postgresql+psycopg://finance_agent:devpassword@localhost:5433/finance_dev"
    )

    plaid_env: str = "sandbox"
    plaid_client_id: str = ""
    plaid_secret: SecretStr = SecretStr("")
    plaid_access_token: SecretStr = SecretStr("")
    plaid_webhook_secret: SecretStr = SecretStr("")

    # Runtime financial agent — provider-interchangeable per ADR-014.
    # Claude Code (the engineering agent building this application) is a
    # separate system from this runtime agent even when both are
    # Claude-API-backed; see CLAUDE.md.
    agent_provider: Literal["openai", "anthropic"] = "openai"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = ""

    log_level: str = "INFO"
    log_format: str = "json"

    def active_agent_credential(self) -> SecretStr:
        """The API key for whichever provider `agent_provider` selects.

        Only the active provider's credential is required at runtime (ADR-014);
        the inactive provider's key may be unset. Callers should surface
        `MissingProviderCredentialError` as a clear startup error rather than
        letting an empty key fail confusingly deep inside an SDK call.
        """
        key = self.openai_api_key if self.agent_provider == "openai" else self.anthropic_api_key
        if not key.get_secret_value():
            raise MissingProviderCredentialError(self.agent_provider)
        return key


class MissingProviderCredentialError(RuntimeError):
    def __init__(self, provider: str) -> None:
        env_var = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        super().__init__(
            f"AGENT_PROVIDER is set to {provider!r} but {env_var} is not configured. "
            f"Set {env_var} or change AGENT_PROVIDER."
        )
        self.provider = provider


def get_settings() -> Settings:
    return Settings()
