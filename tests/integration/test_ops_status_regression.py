"""Adversarial regression tests for `ops/status.py` health reporting
(QA, Milestone 7 — handoff §10, §22, ADR-008).

`finops deploy` gates promotion/auto-rollback entirely on
`status.aggregate_health`. These tests encode confirmed cases where that
function reports `healthy` for a broken system, or crashes instead of
reporting a status. All are `xfail(strict=True)` — remove the marker once
fixed, don't delete the test.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from finance_app.config.settings import Settings
from finance_app.ops import status
from tests.conftest import role_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_unmigrated_database():
    """A real, reachable database that has never had `alembic upgrade head`
    run against it — i.e. the state of the VPS during the very first
    `finops deploy`, per docs/runbooks/deploy.md §3."""
    admin_url = role_dsn("finance_migrator")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = "qa_unmigrated_scratch"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield admin_url.rsplit("/", 1)[0] + f"/{name}"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_migration_status_reports_unmigrated_instead_of_raising(fresh_unmigrated_database) -> None:
    engine = create_engine(fresh_unmigrated_database)
    try:
        with Session(engine) as session:
            result = status.migration_status(session)
    finally:
        engine.dispose()

    assert result["status"] == "unmigrated"
    assert result["applied"] is None


def test_aggregate_health_degrades_rather_than_raising_on_an_unmigrated_database(
    fresh_unmigrated_database,
) -> None:
    engine = create_engine(fresh_unmigrated_database)
    settings = Settings(
        database_url=fresh_unmigrated_database,
        alembic_database_url=fresh_unmigrated_database,
        agent_database_url=fresh_unmigrated_database,
        observer_database_url=fresh_unmigrated_database,
    )
    try:
        with Session(engine) as session:
            result = status.aggregate_health(session, settings)
    finally:
        engine.dispose()

    assert result["overall"] == "unhealthy"
    assert result["migrations"] == "unmigrated"


@pytest.fixture
def clean_backup_runs(role_engine):
    engine = role_engine("finance_app")
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM ops.backup_runs"))
        conn.commit()
    yield engine
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM ops.backup_runs"))
        conn.commit()


def test_a_failing_backup_makes_aggregate_health_unhealthy(clean_backup_runs) -> None:
    now = datetime.datetime.now(datetime.UTC)
    with Session(clean_backup_runs) as session:
        session.execute(
            text(
                "INSERT INTO ops.backup_runs (run_type, status, started_at, finished_at, error) "
                "VALUES ('backup', 'failed', :t, :t, 'pg_dump failed')"
            ),
            {"t": now},
        )
        session.commit()

        settings = Settings(
            database_url=role_dsn("finance_app"),  # type: ignore[arg-type]
            alembic_database_url=role_dsn("finance_migrator"),  # type: ignore[arg-type]
            agent_database_url=role_dsn("finance_agent"),  # type: ignore[arg-type]
            observer_database_url=role_dsn("finance_observer"),  # type: ignore[arg-type]
        )
        result = status.aggregate_health(session, settings)

    assert result["backup"] != "verified"
    assert result["overall"] == "unhealthy", (
        "a database whose last backup failed is not a healthy production system"
    )


def test_aggregate_health_actually_probes_the_deployed_application(role_engine) -> None:
    """`application` must be derived from something, not asserted. As
    written, `status.aggregate_health` returns the literal string
    "healthy" for that key on every call regardless of the running
    container — assert that the key is at least capable of being
    something else."""
    import inspect

    source = inspect.getsource(status.aggregate_health)
    assert '"application": "healthy"' not in source, (
        "aggregate_health hard-codes the application component; it must probe the deployed "
        "container (e.g. `docker compose ps`/an in-container `finance status`) for "
        "`finops deploy`'s health gate to mean anything"
    )
