"""The runtime agent's own database connection — the `finance_agent` role,
never `finance_app`/`finance_owner` (handoff §7.1, ADR-014, docs/security-
model.md invariant 5). Every tool handler in `agent/tools/` runs against a
session from this module. Using `db/session.py`'s `finance_app`-bound
engine here would silently defeat the database-level boundary that keeps
the agent off raw Plaid writes.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from finance_app.config.settings import get_settings

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_agent_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().agent_database_url.get_secret_value(), pool_pre_ping=True
        )
    return _engine


def get_agent_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_agent_engine(), expire_on_commit=False)
    return _sessionmaker


def dispose_agent_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextmanager
def agent_session_scope() -> Iterator[Session]:
    """A transactional `finance_agent` session: commits on clean exit,
    rolls back on error. One call to `run_agent_turn` runs inside one of
    these — a mid-turn failure rolls back every tool-call side effect from
    that turn atomically, which is correct: nothing was surfaced to the
    user yet, so a partial turn should leave no partial trace."""
    session = get_agent_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
