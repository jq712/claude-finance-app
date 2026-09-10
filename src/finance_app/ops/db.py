"""`finops`'s own database connection — the `finance_observer` role, never
`finance_app`/`finance_owner` (handoff §7.1, §10). `finops` diagnoses
production; it does not get application write authority. Every status
function in `ops/status.py` that `finops` calls runs against a session
from this module. `ops/backup.py` and `ops/health.py` are the two
exceptions: they write `ops.backup_runs`/`ops.errors`, which
`finance_observer` cannot do by design, so they use
`finance_app.db.session.session_scope` (the `finance_app` role) instead.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from finance_app.config.settings import get_settings

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def get_observer_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().observer_database_url, pool_pre_ping=True)
    return _engine


def get_observer_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_observer_engine(), expire_on_commit=False)
    return _sessionmaker


def dispose_observer_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextmanager
def observer_session_scope() -> Iterator[Session]:
    """A read-only `finance_observer` session. Commits are harmless no-ops
    here (the role can't write anything outside its narrow grants) but the
    context manager still rolls back cleanly on error for consistency with
    `db/session.py`/`agent/db.py`."""
    session = get_observer_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
