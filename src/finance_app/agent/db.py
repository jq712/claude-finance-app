"""The runtime agent's own database connection — the `finance_agent` role,
never `finance_app`/`finance_owner` (handoff §7.1, ADR-014, docs/security-
model.md invariant 5). Every tool handler in `agent/tools/` runs against a
session from this module. Using `db/session.py`'s `finance_app`-bound
engine here would silently defeat the database-level boundary that keeps
the agent off raw Plaid writes.
"""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from finance_app.config.settings import get_settings

_engine: Engine | None = None
_engine_dsn_fingerprint: str | None = None
_sessionmaker: sessionmaker[Session] | None = None


def _fingerprint(dsn: str) -> str:
    """A hash of the DSN, not the DSN itself — see the matching helper in
    `db/session.py` for why comparing `str(engine.url)` is not safe here."""
    return hashlib.sha256(dsn.encode()).hexdigest()


def get_agent_engine() -> Engine:
    """Rebuilds automatically if `get_settings().agent_database_url` has
    changed since the last call — see `db/session.py:get_engine`'s
    matching docstring for why a module-global cache must not survive a
    settings change silently."""
    global _engine, _engine_dsn_fingerprint
    dsn = get_settings().agent_database_url.get_secret_value()
    fingerprint = _fingerprint(dsn)
    if _engine is None or fingerprint != _engine_dsn_fingerprint:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(dsn, pool_pre_ping=True)
        _engine_dsn_fingerprint = fingerprint
        global _sessionmaker
        _sessionmaker = None
    return _engine


def get_agent_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    engine = get_agent_engine()  # may rebuild _sessionmaker as a side effect
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=engine, expire_on_commit=False)
    return _sessionmaker


def dispose_agent_engine() -> None:
    global _engine, _engine_dsn_fingerprint, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_dsn_fingerprint = None
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
