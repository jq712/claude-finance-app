from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from finance_app.config.settings import get_settings

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """The application's engine, connected as the `finance_app` role.

    Deterministic ingestion and application code use this. It is never used
    to run migrations (that's `finance_migrator`, via `alembic`) and never
    used by the runtime agent's tool layer (that's `finance_agent`, via its
    own connection — see agent/tools/).
    """
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _sessionmaker


def dispose_engine() -> None:
    """Drop pooled connections and force the next `get_engine()`/
    `get_sessionmaker()` call to reconnect from scratch.

    Needed after anything that drops and recreates a database role out
    from under an already-pooled connection — e.g. `alembic downgrade`
    on the roles-and-grants migration. PostgreSQL ties an open session's
    privileges to the role identity at connect time, so a connection
    pooled before a `DROP ROLE` / `CREATE ROLE` round-trip silently loses
    access to grants issued to the new role object, even though the role
    name is unchanged (see
    tests/integration/test_migration_reversibility.py, which calls this
    after restoring migrations to head).
    """
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional session: commits on clean exit, rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
