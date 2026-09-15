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
    """A hash of the DSN, not the DSN itself — comparing `str(engine.url)`
    would mask the password and treat two DSNs differing only by password
    as equal, defeating the point of noticing the DSN changed."""
    return hashlib.sha256(dsn.encode()).hexdigest()


def get_engine() -> Engine:
    """The application's engine, connected as the `finance_app` role.

    Deterministic ingestion and application code use this. It is never used
    to run migrations (that's `finance_migrator`, via `alembic`) and never
    used by the runtime agent's tool layer (that's `finance_agent`, via its
    own connection — see agent/tools/).

    Rebuilds automatically if `get_settings().database_url` has changed
    since the last call (ADR-019: `get_settings()` is deliberately
    uncached, e.g. a test flips `FINANCE_ENV_FILE` mid-process — a module
    global that cached the *first* engine forever would silently keep
    talking to the old database after that)."""
    global _engine, _engine_dsn_fingerprint
    dsn = get_settings().database_url.get_secret_value()
    fingerprint = _fingerprint(dsn)
    if _engine is None or fingerprint != _engine_dsn_fingerprint:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(dsn, pool_pre_ping=True)
        _engine_dsn_fingerprint = fingerprint
        global _sessionmaker
        _sessionmaker = None
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    engine = get_engine()  # may rebuild _sessionmaker as a side effect
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=engine, expire_on_commit=False)
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
    global _engine, _engine_dsn_fingerprint, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_dsn_fingerprint = None
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
