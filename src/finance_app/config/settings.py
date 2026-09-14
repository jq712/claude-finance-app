from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from finance_app.config.env import (
    PRODUCTION_ENV_FILE_VAR,
    ProductionAccessRefusedError,
    resolve_env_file,
)

# The only database name `Settings` will ever resolve a DSN against
# without `FINANCE_ENV_FILE` having been explicitly set (ADR-019's "the
# CLI defaults to the dev DSN, prod only via explicit env path").
_PRODUCTION_DATABASE_NAME = "finance_prod"

# Every field holding a DSN — the validator below checks each of these,
# never a hardcoded list of role names, so a future DSN field is covered
# automatically rather than by remembering to update two places.
_DSN_FIELD_NAMES = (
    "database_url",
    "alembic_database_url",
    "agent_database_url",
    "observer_database_url",
    "backup_database_url",
)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables / .env.

    Never gains a default that points at a production value — see
    CLAUDE.md and docs/security-model.md. Values are read from the
    environment; this repository never contains a real credential.

    Construct via `get_settings()`, not `Settings()` directly, outside of
    tests — `get_settings()` resolves which env file to load from
    `FINANCE_ENV_FILE` (`config/env.py`) and passes along whether that
    resolution was explicit, which `_reject_production_dsn_without_explicit_opt_in`
    below needs to tell a deliberate production connection from an
    accidental one.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Set by `get_settings()` to whatever `config.env.resolve_env_file()`
    # returned as its second element — never itself read from the
    # environment (an env var named e.g. `env_file_explicit` would just
    # move the forgeability problem this field exists to close onto a new
    # name). `exclude=True` so it never round-trips through
    # `model_dump()`/`--json` output.
    env_file_explicit: bool = Field(default=False, exclude=True)

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

    # --- Release identity (Milestone 7, ADR-019) ------------------------
    # The `RELEASE_ID` environment variable this process happened to be
    # started with — set by `finops deploy`/`rollback`/`restart` before
    # every `finance selfcheck` run (`ops.status.probe_release`), never
    # trustworthy as an identity check on its own: the caller that sets
    # this is the same caller `probe_release` is trying to verify, so
    # comparing a release against *this* value can never disagree with
    # what was just injected (QA-37's original bug, under the Docker
    # model). `finops version`/`finance status` fall back to `__version__`
    # when this is empty (e.g. plain local dev).
    release_id: str = ""

    # Filesystem root of the bare-metal release tree (ADR-019):
    # `<release_root>/releases/<sha>/`, `<release_root>/current`. Defaults
    # to the real production path so a systemd unit's environment needs no
    # override, but every `finops` command also accepts `--release-root`,
    # which is what lets the whole deploy path be unit-tested against a
    # `tmp_path` with no `/opt/finance` and no root anywhere in the
    # process — see `ops/host.py`.
    release_root: str = "/opt/finance"

    # systemd units `finops deploy`/`rollback`/`restart` restart after
    # repointing `current`. Deliberately empty by default: the unit files
    # themselves are a later PR (ADR-019's implementation-status table
    # still marks them "Not shipped"), and an empty tuple means "skip the
    # restart and say so" rather than this PR inventing unit names it
    # cannot yet know are right. See `ops/host.py:run_systemctl`.
    production_units: tuple[str, ...] = ()
    # Argv prefix `run_systemctl` invokes each action through — e.g.
    # `("sudo", "-n", "systemctl")` if `finance-prod` needs `sudo` for
    # `systemctl restart`. The `-n` (non-interactive) flag is the
    # operator's responsibility to include: a `sudo` that blocks on a
    # password prompt would hang `finops deploy` exactly the way ADR-016
    # D3's `timeout` parameter was added to prevent for `docker compose`.
    systemctl_prefix: tuple[str, ...] = ("systemctl",)

    log_level: str = "INFO"
    log_format: str = "json"

    @model_validator(mode="after")
    def _reject_production_dsn_without_explicit_opt_in(self) -> "Settings":
        """ADR-019: "the default must be incapable of touching production
        data at all, not merely configured not to." Enforced, not just
        documented — every DSN field is parsed and its database name
        checked; if any names `finance_prod` while this `Settings` was not
        built from an explicitly-set `FINANCE_ENV_FILE` naming
        `finance_env=production`, construction itself fails.

        Both conditions are required, not just one: `env_file_explicit`
        alone would let an operator accidentally leave `FINANCE_ENV_FILE`
        pointed at a stale path; `finance_env == "production"` alone would
        let a plain shell export of `DATABASE_URL=...finance_prod...`
        (with no env file at all) sail through, since nothing before this
        validator ever reads `finance_env`. Requiring both closes the gap
        `finance_env` being dead config (declared, never read anywhere in
        `src/`) previously left open.
        """
        if self.finance_env == "production" and self.env_file_explicit:
            return self
        for field_name in _DSN_FIELD_NAMES:
            dsn = getattr(self, field_name).get_secret_value()
            if not dsn:
                continue
            try:
                database_name = make_url(dsn).database
            except Exception:  # noqa: BLE001 - an unparseable DSN is not this
                # validator's problem to diagnose; SQLAlchemy/psycopg will
                # raise their own clear error the moment it's actually used.
                continue
            if database_name == _PRODUCTION_DATABASE_NAME:
                raise ProductionAccessRefusedError(
                    f"{field_name} names the {_PRODUCTION_DATABASE_NAME!r} database, but "
                    f"{PRODUCTION_ENV_FILE_VAR} was not explicitly set (or its file's "
                    "FINANCE_ENV is not 'production'). Refusing to construct Settings that "
                    "could touch production data by accident — see ADR-019."
                )
        return self

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
    """Constructs `Settings` from whichever env file `FINANCE_ENV_FILE`
    (if set) or `.env` (the default) resolves to — see `config/env.py`.

    Deliberately *not* cached: a settings change (e.g. a test that sets
    `FINANCE_ENV_FILE` mid-process) must take effect on the next call.
    `db/session.py`/`ops/db.py` cache the *engine* they build from a given
    settings' DSN, keyed by the DSN itself, so this staying uncached does
    not mean a fresh connection pool every call — only a fresh read of
    what the DSN currently is.
    """
    path, explicit = resolve_env_file()
    return Settings(_env_file=path, env_file_explicit=explicit)
