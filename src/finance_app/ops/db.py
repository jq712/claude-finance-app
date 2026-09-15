"""`finops`'s own database connection — the `finance_observer` role, never
`finance_app`/`finance_owner` (handoff §7.1, §10). `finops` diagnoses
production; it does not get application write authority. Every status
function in `ops/status.py` that `finops` calls runs against a session
from this module. `ops/backup.py` and `ops/health.py` are the two
exceptions: they write `ops.backup_runs`/`ops.errors`, which
`finance_observer` cannot do by design, so they use
`finance_app.db.session.session_scope` (the `finance_app` role) instead.
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


def get_observer_engine() -> Engine:
    """Rebuilds automatically if `get_settings().observer_database_url` has
    changed since the last call — see `db/session.py:get_engine`'s
    matching docstring for why a module-global cache must not survive a
    settings change silently."""
    global _engine, _engine_dsn_fingerprint
    dsn = get_settings().observer_database_url.get_secret_value()
    fingerprint = _fingerprint(dsn)
    if _engine is None or fingerprint != _engine_dsn_fingerprint:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(dsn, pool_pre_ping=True)
        _engine_dsn_fingerprint = fingerprint
        global _sessionmaker
        _sessionmaker = None
    return _engine


def get_observer_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    engine = get_observer_engine()  # may rebuild _sessionmaker as a side effect
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=engine, expire_on_commit=False)
    return _sessionmaker


def dispose_observer_engine() -> None:
    global _engine, _engine_dsn_fingerprint, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_dsn_fingerprint = None
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
