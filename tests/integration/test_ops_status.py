"""Integration tests for the status-gathering functions behind every
`finops` read command (handoff §10, `src/finance_app/ops/status.py`)."""

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.db.models.ops import BackupRun, OperationalError, SyncRun
from finance_app.ops import status

pytestmark = pytest.mark.integration


@pytest.fixture
def db_session(role_engine):
    engine = role_engine("finance_app")
    with Session(engine) as session:
        yield session
        session.rollback()
        with engine.connect() as conn:
            conn.execute(
                text(
                    "DELETE FROM ops.sync_runs; DELETE FROM ops.backup_runs; "
                    "DELETE FROM ops.errors; DELETE FROM plaid.items;"
                )
            )
            conn.commit()


def test_db_status_healthy(db_session: Session) -> None:
    assert status.db_status(db_session) == {"status": "healthy", "detail": None}


def test_migration_status_up_to_date(db_session: Session) -> None:
    result = status.migration_status(db_session)
    assert result["status"] == "up_to_date"
    assert result["applied"] == result["head"]


def test_sync_status_never_run(db_session: Session) -> None:
    assert status.sync_status(db_session) == {
        "status": "never_run",
        "last_run": None,
        "item_status": None,
    }


def test_sync_status_reports_most_recent_successful_run(db_session: Session) -> None:
    item_id = db_session.execute(
        text(
            "INSERT INTO plaid.items (plaid_item_id, institution_name) "
            "VALUES ('status-test-item', 'Test Bank') RETURNING id"
        )
    ).scalar_one()
    db_session.flush()

    run = SyncRun(
        item_id=item_id,
        run_type="scheduled",
        status="success",
        finished_at=datetime.datetime.now(datetime.UTC),
        added_count=3,
        modified_count=1,
        removed_count=0,
    )
    db_session.add(run)
    db_session.flush()

    result = status.sync_status(db_session)

    assert result["status"] == "success"
    assert result["last_run"]["added_count"] == 3
    assert result["item_status"] == "active"


def test_sync_status_flags_a_stale_successful_run(db_session: Session) -> None:
    item_id = db_session.execute(
        text(
            "INSERT INTO plaid.items (plaid_item_id, institution_name) "
            "VALUES ('stale-test-item', 'Test Bank') RETURNING id"
        )
    ).scalar_one()
    db_session.flush()

    stale_finish = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
    run = SyncRun(
        item_id=item_id,
        run_type="scheduled",
        status="success",
        finished_at=stale_finish,
    )
    db_session.add(run)
    db_session.flush()

    result = status.sync_status(db_session)

    assert result["status"] == "stale"


def test_sync_status_reports_error_status_as_is(db_session: Session) -> None:
    item_id = db_session.execute(
        text(
            "INSERT INTO plaid.items (plaid_item_id, institution_name) "
            "VALUES ('error-test-item', 'Test Bank') RETURNING id"
        )
    ).scalar_one()
    db_session.flush()

    run = SyncRun(
        item_id=item_id,
        run_type="scheduled",
        status="error",
        finished_at=datetime.datetime.now(datetime.UTC),
        last_error="ITEM_LOGIN_REQUIRED",
    )
    db_session.add(run)
    db_session.flush()

    result = status.sync_status(db_session)

    assert result["status"] == "error"
    assert result["last_run"]["last_error"] == "ITEM_LOGIN_REQUIRED"


def test_backup_status_never_run(db_session: Session) -> None:
    assert status.backup_status(db_session) == {
        "status": "never_run",
        "last_backup": None,
        "last_verification": None,
    }


def test_backup_status_unverified_without_a_verification_row(db_session: Session) -> None:
    backup = BackupRun(
        run_type="backup",
        status="success",
        finished_at=datetime.datetime.now(datetime.UTC),
        artifact_path="/backups/x.gpg",
    )
    db_session.add(backup)
    db_session.flush()

    result = status.backup_status(db_session)

    assert result["status"] == "unverified"
    assert result["last_verification"] is None


def test_backup_status_verified_with_a_recent_successful_verification(db_session: Session) -> None:
    backup = BackupRun(
        run_type="backup",
        status="success",
        finished_at=datetime.datetime.now(datetime.UTC),
        artifact_path="/backups/x.gpg",
    )
    db_session.add(backup)
    db_session.flush()

    verification = BackupRun(
        run_type="restore_verification",
        status="success",
        finished_at=datetime.datetime.now(datetime.UTC),
        verifies_backup_id=backup.id,
    )
    db_session.add(verification)
    db_session.flush()

    result = status.backup_status(db_session)

    assert result["status"] == "verified"


def test_backup_status_unverified_if_verification_is_stale(db_session: Session) -> None:
    backup = BackupRun(
        run_type="backup",
        status="success",
        finished_at=datetime.datetime.now(datetime.UTC),
        artifact_path="/backups/x.gpg",
    )
    db_session.add(backup)
    db_session.flush()

    stale_finish = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    verification = BackupRun(
        run_type="restore_verification",
        status="success",
        finished_at=stale_finish,
        verifies_backup_id=backup.id,
    )
    db_session.add(verification)
    db_session.flush()

    result = status.backup_status(db_session)

    assert result["status"] == "unverified"


def test_recent_errors_returns_sanitized_rows_newest_first(db_session: Session) -> None:
    older = OperationalError(category="sync", message="first")
    db_session.add(older)
    db_session.flush()
    newer = OperationalError(category="sync", message="second")
    db_session.add(newer)
    db_session.flush()

    result = status.recent_errors(db_session, limit=10)

    assert [row["message"] for row in result[:2]] == ["second", "first"]


def test_aggregate_health_is_healthy_with_no_sync_or_backup_history(db_session: Session) -> None:
    result = status.aggregate_health(db_session)

    assert result["overall"] == "healthy"
    assert result["sync"] == "never_run"
    assert result["backup"] == "never_run"


def test_aggregate_health_is_unhealthy_when_sync_errored(db_session: Session) -> None:
    item_id = db_session.execute(
        text(
            "INSERT INTO plaid.items (plaid_item_id, institution_name) "
            "VALUES ('health-error-item', 'Test Bank') RETURNING id"
        )
    ).scalar_one()
    db_session.flush()
    run = SyncRun(
        item_id=item_id,
        run_type="scheduled",
        status="error",
        finished_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(run)
    db_session.flush()

    result = status.aggregate_health(db_session)

    assert result["overall"] == "unhealthy"
    assert result["sync"] == "error"
