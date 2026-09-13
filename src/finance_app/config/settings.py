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

    # DSN fields are `SecretStr`, not plain `str` (they embed a role
    # password) — a plain string field is exactly the kind of value
    # `ops/logging.py`'s `sanitize_context` can miss if it's ever passed
    # through structured logging by key name alone; `SecretStr` makes the
    # raw value opaque by construction (`repr()`/`str()` both redact it,
    # only `.get_secret_value()` reveals it) as defense in depth on top of
    # that redaction. Every call site does `.get_secret_value()` once, at
    # the point it hands the DSN to SQLAlchemy/libpq.
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://finance_app:devpassword@localhost:5433/finance_dev"
    )
    # Migrations run DDL and create roles, so they use the bootstrap/owner
    # role rather than the least-privilege `finance_app` runtime role.
    alembic_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://finance_migrator:devpassword@localhost:5433/finance_dev"
    )
    # The runtime agent's own least-privilege connection (handoff §7.1,
    # ADR-014). Deliberately separate from `database_url` (`finance_app`,
    # which can write plaid.*) — using the wrong connection here would
    # silently defeat the database-level boundary that keeps the agent off
    # raw Plaid facts. See docs/security-model.md invariant 5.
    agent_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://finance_agent:devpassword@localhost:5433/finance_dev"
    )

    # `finops`'s own connection (handoff §10): sanitized, read-only
    # operational data only — ops.* plus the narrow plaid.items/sync_state
    # grants added in migrations/versions/0004. Deliberately never
    # database_url/finance_app: finops diagnoses production, it does not
    # get application write authority. See docs/security-model.md.
    observer_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://finance_observer:devpassword@localhost:5433/finance_dev"
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

    # --- Backups (Milestone 7, ADR-015) --------------------------------
    # `finance_backup` is read-only everywhere (migrations/versions/0002),
    # so pg_dump always runs as this role, never as finance_app/finance_owner.
    backup_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://finance_backup:devpassword@localhost:5433/finance_dev"
    )
    # Passphrase for symmetric GPG encryption of backup archives before they
    # leave the VPS (ADR-015). Production value is a systemd encrypted
    # credential; dev/CI use a synthetic value and never a real secret.
    backup_encryption_key: SecretStr = SecretStr("")
    # Directory backups/restores are staged in. In production this is a
    # host-mounted volume the backup timer writes to; off-machine transfer
    # of that directory's contents is an owner-performed operational step
    # documented in docs/backups.md (deliberately not automated here — see
    # that doc for why).
    backup_dir: str = "./backups"

    # --- Release identity (Milestone 7, ADR-008) ------------------------
    # The immutable Git SHA this running container was built from. Set by
    # `deploy/compose.yaml` from the image tag; empty means "not running
    # from a release image" (e.g. local dev), in which case `finops
    # version`/health reporting falls back to __version__.
    release_id: str = ""
    # QA-37: baked into the image at build time (Dockerfile `ARG RELEASE_ID`
    # + `ENV IMAGE_RELEASE_ID=$RELEASE_ID`), never set by
    # `deploy/compose.yaml` — unlike `release_id` above, which is merely
    # the `RELEASE_ID` *environment variable* `docker compose run` was
    # invoked with, and which `probe_release` itself sets before every
    # selfcheck run, this value cannot be influenced by the process that
    # starts the container. `probe_release`'s `wrong_image` check compares
    # this against the requested release id; comparing `release_id`
    # instead (as it used to) can never disagree with what was just
    # injected, so it could never actually detect a wrong image.
    image_release_id: str = ""
    # `ghcr.io/<owner>/<repo>` — CI publishes `container_image_repo:<sha>`
    # (.github/workflows/ci.yml); `finops deploy`/`rollback` build the full
    # image ref from this plus a release id.
    container_image_repo: str = "ghcr.io/jq712/claude-finance-app"

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
