import os

import pytest
from sqlalchemy import Engine, create_engine

# `FINANCE_*_DB_PASSWORD`/`PLAID_*` are the credential env vars
# `/opt/finance/.env` supplies in production (ADR-019; formerly
# `deploy/compose.yaml`'s `${VAR:?required}` interpolation, now removed).
# `tests/unit/test_ops_host_env_regression.py` (formerly
# `test_ops_compose_env_regression.py`) exercises `run_release`'s real
# environment-merging behavior (QA-1), which needs the same shape of
# ambient environment to prove anything; `deploy/scripts/with-production-env.sh`
# and `deploy/scripts/finops.sh` are the production consumers of this exact
# variable set. `setdefault` so a real CI/job-level value (or a developer's
# own `.env` export) is never overridden.
_RELEASE_ENV_DEFAULTS = {
    "FINANCE_MIGRATOR_DB_PASSWORD": "devpassword",
    "FINANCE_APP_DB_PASSWORD": "devpassword",
    "FINANCE_AGENT_DB_PASSWORD": "devpassword",
    "FINANCE_OBSERVER_DB_PASSWORD": "devpassword",
    "FINANCE_BACKUP_DB_PASSWORD": "devpassword",
    "PLAID_CLIENT_ID": "test-placeholder",
    "PLAID_SECRET": "test-placeholder",
    "PLAID_ACCESS_TOKEN": "test-placeholder",
}
for _key, _value in _RELEASE_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


def role_dsn(role: str) -> str:
    """Connection string for one of the least-privilege roles created by
    migrations/versions/0002_..._roles_and_grants.py. Password resolution
    mirrors that migration: `<ROLE>_DB_PASSWORD` env var, else the
    synthetic dev-container default."""
    password = os.environ.get(f"{role.upper()}_DB_PASSWORD", "devpassword")
    return f"postgresql+psycopg://{role}:{password}@localhost:5433/finance_dev"


@pytest.fixture
def role_engine():
    """Factory fixture: role_engine("finance_agent") -> Engine, disposed
    after the test."""
    engines: list[Engine] = []

    def _make(role: str) -> Engine:
        engine = create_engine(role_dsn(role))
        engines.append(engine)
        return engine

    yield _make

    for engine in engines:
        engine.dispose()
