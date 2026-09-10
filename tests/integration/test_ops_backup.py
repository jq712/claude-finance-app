"""Integration tests for the backup/restore/verify pipeline (handoff §22,
ADR-015) against the real dev/CI Postgres container — this is the
"backup restore test succeeds" exit criterion for Milestone 7 (handoff
§29). Exercises the exact code path `deploy/scripts/backup.sh`/
`restore.sh`/`restore-verify.sh` invoke inside the built image
(`src/finance_app/ops/backup.py`), not a re-implementation of it.

Requires `pg_dump`/`pg_restore`/`gpg` on PATH — see
`.github/workflows/ci.yml`'s `integration-tests` job for how CI installs
them (postgresql-client-17, matching the service container, plus gnupg).
Skips gracefully if they're not available, so a bare `uv run pytest`
outside that CI step (or without the matching client tools installed
locally) doesn't fail the whole suite over a missing binary rather than a
code defect.
"""

import shutil

import pytest
from sqlalchemy import create_engine, text

from finance_app.config.settings import Settings
from finance_app.ops.backup import (
    BackupError,
    create_backup,
    latest_successful_backup,
    restore_backup,
    verify_restore,
)
from tests.conftest import role_dsn

pytestmark = pytest.mark.integration

_MISSING_TOOLS = [tool for tool in ("pg_dump", "pg_restore", "gpg") if shutil.which(tool) is None]


@pytest.fixture(autouse=True)
def _require_backup_tools():
    if _MISSING_TOOLS:
        pytest.skip(f"missing required binaries on PATH: {', '.join(_MISSING_TOOLS)}")


@pytest.fixture(autouse=True)
def _clean_backup_runs():
    """Most tests below call `create_backup` without ever verifying the
    result, deliberately (that's what a *different* test exercises) —
    left in place, that row is the most recent `ops.backup_runs` entry
    and makes `status.backup_status`/`aggregate_health` report
    `"unverified"` for every other test/command sharing this database
    (e.g. `tests/integration/test_finops_cli.py`'s `finops health` check)
    until something else happens to clean it up. Isolate this file's own
    rows instead of leaking them."""
    engine = create_engine(role_dsn("finance_app"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.backup_runs"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.backup_runs"))
    engine.dispose()


@pytest.fixture
def backup_settings(tmp_path) -> Settings:
    return Settings(
        database_url=role_dsn("finance_app"),  # type: ignore[arg-type]
        alembic_database_url=role_dsn("finance_migrator"),  # type: ignore[arg-type]
        agent_database_url=role_dsn("finance_agent"),  # type: ignore[arg-type]
        backup_database_url=role_dsn("finance_backup"),  # type: ignore[arg-type]
        backup_encryption_key="test-only-passphrase-not-a-real-secret",  # type: ignore[arg-type]
        backup_dir=str(tmp_path / "backups"),
    )


@pytest.fixture
def scratch_database_url():
    """A throwaway database on the same server, dropped after the test —
    stands in for deploy/scripts/restore-verify.sh's throwaway container.
    Never the real finance_dev database: `pg_restore --clean` would drop
    every other integration test's fixtures."""
    admin_engine_url = role_dsn("finance_migrator")
    from sqlalchemy import create_engine

    admin_engine = create_engine(admin_engine_url, isolation_level="AUTOCOMMIT")
    db_name = "finance_backup_test_scratch"
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    target_url = admin_engine_url.rsplit("/", 1)[0] + f"/{db_name}"
    yield target_url
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin_engine.dispose()


def test_create_backup_produces_an_encrypted_artifact_and_records_it(backup_settings) -> None:
    result = create_backup(backup_settings)

    assert result.artifact_path.exists()
    assert result.artifact_path.suffix == ".gpg"
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    # No plaintext dump left behind alongside the encrypted artifact.
    plaintext_candidates = list(result.artifact_path.parent.glob("*.dump.tmp"))
    assert plaintext_candidates == []


def test_create_backup_refuses_to_run_without_an_encryption_key(backup_settings) -> None:
    from pydantic import SecretStr

    unencrypted = backup_settings.model_copy(update={"backup_encryption_key": SecretStr("")})
    with pytest.raises(BackupError, match="BACKUP_ENCRYPTION_KEY"):
        create_backup(unencrypted)


def test_backup_restore_verify_round_trip(backup_settings, scratch_database_url) -> None:
    """The full pipeline: encrypted backup -> restore into a scratch
    database -> post-restore sanity checks pass and agree with the
    counts captured at backup time."""
    backup = create_backup(backup_settings)

    restore_backup(backup.artifact_path, scratch_database_url, backup_settings)

    verification = verify_restore(backup.backup_run_id, scratch_database_url, backup_settings)

    assert verification.status == "success"
    assert verification.details["alembic_version"] is not None
    assert verification.details.get("row_count_mismatches") is None
    assert verification.details["row_counts"] == backup.row_counts


def test_verify_restore_fails_loudly_on_a_row_count_mismatch(
    backup_settings, scratch_database_url
) -> None:
    """A restored database that doesn't match the backup's recorded
    counts must fail verification, not report success — this is the
    entire point of the sanity check existing."""
    from sqlalchemy import create_engine

    backup = create_backup(backup_settings)
    restore_backup(backup.artifact_path, scratch_database_url, backup_settings)

    tamper_engine = create_engine(scratch_database_url)
    with tamper_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO \"user\".preferences (key, value) VALUES ('tamper_test', '{}'::jsonb)"
            )
        )
        conn.commit()
    tamper_engine.dispose()

    verification = verify_restore(backup.backup_run_id, scratch_database_url, backup_settings)

    assert verification.status == "failed"
    mismatches = verification.details.get("row_count_mismatches", {})
    assert isinstance(mismatches, dict)
    assert mismatches.get("user.preferences") == {"expected": 0, "actual": 1}


def test_latest_successful_backup_reflects_the_most_recent_success(backup_settings) -> None:
    first = create_backup(backup_settings)
    second = create_backup(backup_settings)

    latest = latest_successful_backup()

    assert latest is not None
    assert latest.backup_run_id == second.backup_run_id
    assert latest.backup_run_id != first.backup_run_id
    assert latest.artifact_path == str(second.artifact_path)
