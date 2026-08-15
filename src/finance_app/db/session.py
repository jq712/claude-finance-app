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
