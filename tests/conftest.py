import os

import pytest
from sqlalchemy import Engine, create_engine


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
