"""Adversarial regression tests for the backup/restore/verify pipeline
(QA, Milestone 7 — handoff §22, ADR-015).

Complements `tests/integration/test_ops_backup.py`, which covers the happy
path and a tampered-target mismatch but never exercises a backup taken
while the application is writing, artifact permissions, or a credential
whose characters are not URL-safe.

All marked `xfail(strict=True)`: they fail today, and will fail loudly as
XPASS once fixed — remove the marker then.
"""

from __future__ import annotations

import shutil
import stat
import threading
import time

import pytest
from sqlalchemy import create_engine, text

from finance_app.config.settings import Settings
from finance_app.ops.backup import _to_libpq_url, create_backup, restore_backup, verify_restore
from tests.conftest import role_dsn

pytestmark = pytest.mark.integration

_MISSING_TOOLS = [tool for tool in ("pg_dump", "pg_restore", "gpg") if shutil.which(tool) is None]


@pytest.fixture(autouse=True)
def _require_backup_tools():
    if _MISSING_TOOLS:
        pytest.skip(f"missing required binaries on PATH: {', '.join(_MISSING_TOOLS)}")


@pytest.fixture(autouse=True)
def _clean_backup_runs():
    """Tests here call `create_backup` without necessarily verifying the
    result — left in place, an unverified row is the most recent
    `ops.backup_runs` entry and makes `status.backup_status`/
    `aggregate_health` report `"unverified"` for every other test/command
    sharing this database. Isolate this file's own rows."""
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
        database_url=role_dsn("finance_app"),
        alembic_database_url=role_dsn("finance_migrator"),
        agent_database_url=role_dsn("finance_agent"),
        backup_database_url=role_dsn("finance_backup"),
        backup_encryption_key="test-only-passphrase-not-a-real-secret",  # type: ignore[arg-type]
        backup_dir=str(tmp_path / "backups"),
    )


@pytest.fixture
def scratch_database_url():
    admin_url = role_dsn("finance_migrator")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = "qa_backup_regression_scratch"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield admin_url.rsplit("/", 1)[0] + f"/{name}"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def clean_qa_preferences():
    engine = create_engine(role_dsn("finance_app"))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM \"user\".preferences WHERE key LIKE 'qa\\_%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM \"user\".preferences WHERE key LIKE 'qa\\_%'"))
    engine.dispose()


def test_a_backup_taken_during_write_traffic_still_verifies(
    backup_settings, scratch_database_url, clean_qa_preferences
) -> None:
    engine = clean_qa_preferences
    stop = threading.Event()
    counter = {"n": 0}

    def writer() -> None:
        local = create_engine(role_dsn("finance_app"))
        try:
            while not stop.is_set():
                with local.begin() as conn:
                    conn.execute(
                        text(
                            'INSERT INTO "user".preferences (key, value) '
                            "VALUES (:k, '{}'::jsonb) ON CONFLICT DO NOTHING"
                        ),
                        {"k": f"qa_conc_{counter['n']}"},
                    )
                counter["n"] += 1
                time.sleep(0.005)
        finally:
            local.dispose()

    with engine.begin() as conn:
        for i in range(25):
            conn.execute(
                text(
                    'INSERT INTO "user".preferences (key, value) '
                    "VALUES (:k, '{}'::jsonb) ON CONFLICT DO NOTHING"
                ),
                {"k": f"qa_seed_{i}"},
            )

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        backup = create_backup(backup_settings)
    finally:
        stop.set()
        thread.join(timeout=10)

    restore_backup(backup.artifact_path, scratch_database_url, backup_settings)
    verification = verify_restore(backup.backup_run_id, scratch_database_url, backup_settings)

    assert verification.status == "success", (
        "a valid backup taken concurrently with ordinary application writes must verify; "
        f"got {verification.details.get('row_count_mismatches')}"
    )


def test_backup_artifacts_are_not_world_readable(backup_settings) -> None:
    backup = create_backup(backup_settings)

    artifact_mode = stat.S_IMODE(backup.artifact_path.stat().st_mode)
    dir_mode = stat.S_IMODE(backup.artifact_path.parent.stat().st_mode)

    assert artifact_mode & 0o077 == 0, (
        f"encrypted backup artifact is mode {artifact_mode:o}; group/other must have no access"
    )
    assert dir_mode & 0o077 == 0, (
        f"backup staging directory is mode {dir_mode:o}; the plaintext pg_dump file is written "
        "here before encryption"
    )


def test_libpq_url_survives_a_password_generated_the_way_the_runbook_prescribes() -> None:
    from urllib.parse import urlsplit

    password = "ab/cd+ef="  # shape of `openssl rand -base64 32` output
    sqlalchemy_url = f"postgresql+psycopg://finance_backup:{password}@postgres:5432/finance"  # noqa: S105

    libpq_url = _to_libpq_url(sqlalchemy_url)
    parts = urlsplit(libpq_url)

    assert parts.hostname == "postgres", f"libpq would parse the host as {parts.hostname!r}"
    assert parts.port == 5432, f"libpq would parse the port as {parts.port!r}"
    assert parts.path == "/finance"
