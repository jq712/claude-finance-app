"""Backup / restore / restore-verification (handoff §22, ADR-015).

Three operations, deliberately kept as plain importable functions rather
than shell one-liners, so they are unit-testable and so the *only* place
backup logic lives is here — `deploy/scripts/backup.sh` etc. are thin
wrappers that invoke this module's CLI (`python -m finance_app.ops.backup
...`), run by the `finance-backup`/`finance-health` systemd units.

Pipeline for a backup:

    pg_dump (as finance_backup, read-only role)
        -> plaintext .dump file in a private temp location
        -> gpg --symmetric encryption (ADR-015)
        -> plaintext deleted
        -> sha256 + size recorded
        -> ops.backup_runs row written (as finance_app; finance_backup
           cannot write, by design — see migrations/versions/0002)

Restore and verification are separate operations, run against a *scratch*
database — never production — so periodic restore drills can prove a
backup is actually usable without touching real data. See
docs/backups.md and docs/runbooks/restore-backup.md for the operator
procedure this module implements.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import Engine, create_engine

from finance_app.config.settings import Settings, get_settings
from finance_app.db.models.ops import BackupRun
from finance_app.db.session import session_scope

# Representative tables across every schema, used as a lightweight sanity
# signal both at backup time (captured counts) and at verify time
# (restored counts must match). Not exhaustive — a full content diff is
# what pg_dump/pg_restore themselves guarantee; this is the "did the
# restore actually produce a database with data in it" check.
SANITY_TABLES = (
    "plaid.items",
    "plaid.accounts",
    "plaid.transactions",
    "user.preferences",
    "finance.budgets",
    "agent.tool_calls",
)


def _quote_qualified(table: str) -> str:
    """`schema.table` -> `"schema"."table"`. `user` is a reserved word in
    PostgreSQL (the `user.*` schema — handoff §7), so every sanity-table
    reference must be quoted, not just that one."""
    schema, _, name = table.partition(".")
    return f'"{schema}"."{name}"'


class BackupError(RuntimeError):
    """A backup/restore/verify step failed. The message is always built
    from sanitized, argv-only detail — see `_run` — never from a
    passphrase or database URL with embedded credentials."""


@dataclass(frozen=True)
class BackupResult:
    backup_run_id: int
    artifact_path: Path
    size_bytes: int
    sha256: str
    row_counts: dict[str, int]


@dataclass(frozen=True)
class VerificationResult:
    backup_run_id: int
    status: str  # "success" | "failed"
    details: dict[str, object]


def _to_libpq_url(sqlalchemy_url: str) -> str:
    """`postgresql+psycopg://...` -> `postgresql://...`, the form
    `pg_dump`/`pg_restore` (libpq) understand. No credential ever needs to
    be logged to do this — it's a fixed string replace."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _run(command: list[str], *, input_text: str | None = None) -> None:
    """Run a subprocess, raising `BackupError` with a sanitized message on
    failure. `command` must never contain a secret in argv — passphrases
    are always passed via stdin (`input_text`) so they never appear in
    `ps`, shell history, or an error message built from `command`."""
    try:
        subprocess.run(  # noqa: S603 - fixed argv built by this module, never from user input
            command,
            input=input_text,
            text=True,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # stderr from pg_dump/gpg can be verbose but is not expected to
        # contain secrets (they're passed via stdin/env, not argv/output);
        # still cap length defensively before it reaches ops.backup_runs.error.
        raise BackupError(
            f"{command[0]} failed (exit {exc.returncode}): {exc.stderr[:500]}"
        ) from exc


def _sanity_row_counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in SANITY_TABLES:
            result = conn.execute(
                text(f"SELECT count(*) FROM {_quote_qualified(table)}")  # noqa: S608
            )
            counts[table] = result.scalar_one()
    return counts


def create_backup(settings: Settings | None = None) -> BackupResult:
    """Dump the database (as `finance_backup`, read-only — ADR-015),
    encrypt it, and record the result in `ops.backup_runs`."""
    settings = settings or get_settings()
    passphrase = settings.backup_encryption_key.get_secret_value()
    if not passphrase:
        raise BackupError(
            "BACKUP_ENCRYPTION_KEY is not set. Refusing to write an unencrypted "
            "backup — see docs/backups.md and ADR-015."
        )

    output_dir = Path(settings.backup_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    plaintext_path = output_dir / f".finance-{timestamp}.dump.tmp"
    encrypted_path = output_dir / f"finance-{timestamp}.dump.gpg"

    with session_scope() as session:
        run = BackupRun(run_type="backup", status="running")
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        backup_engine = create_engine(settings.backup_database_url)
        try:
            row_counts = _sanity_row_counts(backup_engine)
        finally:
            backup_engine.dispose()

        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={plaintext_path}",
                "--dbname",
                _to_libpq_url(settings.backup_database_url),
            ]
        )
        _run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "--output",
                str(encrypted_path),
                str(plaintext_path),
            ],
            input_text=passphrase,
        )
        size_bytes = encrypted_path.stat().st_size
        sha256 = hashlib.sha256(encrypted_path.read_bytes()).hexdigest()

        with session_scope() as session:
            run = session.get(BackupRun, run_id)
            assert run is not None
            run.status = "success"
            run.finished_at = datetime.datetime.now(datetime.UTC)
            run.artifact_path = str(encrypted_path)
            run.size_bytes = size_bytes
            run.sha256 = sha256
            run.source_row_counts = row_counts

        return BackupResult(
            backup_run_id=run_id,
            artifact_path=encrypted_path,
            size_bytes=size_bytes,
            sha256=sha256,
            row_counts=row_counts,
        )
    except BackupError as exc:
        with session_scope() as session:
            run = session.get(BackupRun, run_id)
            assert run is not None
            run.status = "failed"
            run.finished_at = datetime.datetime.now(datetime.UTC)
            run.error = str(exc)[:2000]
        raise
    finally:
        plaintext_path.unlink(missing_ok=True)


def restore_backup(
    encrypted_path: Path,
    target_database_url: str,
    settings: Settings | None = None,
) -> None:
    """Decrypt and `pg_restore` an encrypted backup into `target_database_url`.

    `target_database_url` must be a scratch database, never production —
    `pg_restore --clean` drops existing objects first. Callers (the
    restore-verification drill, an owner-performed disaster recovery)
    are responsible for pointing this at a database that is safe to
    overwrite; see docs/runbooks/restore-backup.md.
    """
    settings = settings or get_settings()
    passphrase = settings.backup_encryption_key.get_secret_value()
    if not passphrase:
        raise BackupError("BACKUP_ENCRYPTION_KEY is not set; cannot decrypt.")

    plaintext_path = encrypted_path.with_suffix(".restore.tmp")
    try:
        _run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--quiet",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--decrypt",
                "--output",
                str(plaintext_path),
                str(encrypted_path),
            ],
            input_text=passphrase,
        )
        _run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                f"--dbname={_to_libpq_url(target_database_url)}",
                str(plaintext_path),
            ]
        )
    finally:
        plaintext_path.unlink(missing_ok=True)


def verify_restore(
    backup_run_id: int,
    target_database_url: str,
    settings: Settings | None = None,
) -> VerificationResult:
    """Post-restore sanity checks against `target_database_url`: the
    schema is present, `alembic_version` exists, and the sanity-table row
    counts match what was recorded when the backup was taken. Records a
    `restore_verification` row in `ops.backup_runs` (in the *primary*
    database via `database_url`, not the scratch target) linked to the
    backup it verified — this is what makes `finops backup-status` able to
    say "verified" rather than merely "a file exists somewhere".
    """
    settings = settings or get_settings()

    with session_scope() as session:
        source = session.get(BackupRun, backup_run_id)
        if source is None:
            raise BackupError(f"No backup_runs row with id={backup_run_id}")
        expected_counts = dict(source.source_row_counts or {})

    with session_scope() as session:
        verification = BackupRun(
            run_type="restore_verification",
            status="running",
            verifies_backup_id=backup_run_id,
        )
        session.add(verification)
        session.flush()
        verification_id = verification.id

    details: dict[str, object] = {}
    ok = True
    engine = create_engine(target_database_url)
    try:
        with engine.connect() as conn:
            try:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
                details["alembic_version"] = version
            except Exception as exc:  # noqa: BLE001 - any failure here is a verification failure
                details["alembic_version_error"] = type(exc).__name__
                ok = False
                version = None

            actual_counts: dict[str, int] = {}
            for table in SANITY_TABLES:
                try:
                    actual_counts[table] = conn.execute(
                        text(f"SELECT count(*) FROM {_quote_qualified(table)}")  # noqa: S608
                    ).scalar_one()
                except Exception as exc:  # noqa: BLE001
                    actual_counts[table] = -1
                    details.setdefault("row_count_errors", {})[table] = type(exc).__name__  # type: ignore[index]
                    ok = False
            details["row_counts"] = actual_counts

            mismatches = {
                table: {"expected": expected_counts.get(table), "actual": actual}
                for table, actual in actual_counts.items()
                if expected_counts.get(table) is not None and expected_counts[table] != actual
            }
            if mismatches:
                details["row_count_mismatches"] = mismatches
                ok = False
    finally:
        engine.dispose()

    status = "success" if ok else "failed"
    with session_scope() as session:
        row = session.get(BackupRun, verification_id)
        assert row is not None
        row.status = status
        row.finished_at = datetime.datetime.now(datetime.UTC)
        row.verification_details = details
        if not ok:
            row.error = "Restore verification sanity checks failed; see verification_details."

    return VerificationResult(backup_run_id=verification_id, status=status, details=details)


@dataclass(frozen=True)
class LatestBackup:
    backup_run_id: int
    artifact_path: str


def latest_successful_backup() -> LatestBackup | None:
    """The most recent successful `backup` row, for `deploy/scripts/
    restore-verify.sh`'s weekly drill — it doesn't know a backup id ahead
    of time, only "verify whatever the most recent backup was"."""
    with session_scope() as session:
        row = (
            session.execute(
                select(BackupRun)
                .where(BackupRun.run_type == "backup", BackupRun.status == "success")
                .order_by(BackupRun.started_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None or row.artifact_path is None:
            return None
        return LatestBackup(backup_run_id=row.id, artifact_path=row.artifact_path)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m finance_app.ops.backup")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="Create an encrypted backup and record it.")

    restore_parser = sub.add_parser("restore", help="Decrypt and restore into a target database.")
    restore_parser.add_argument("encrypted_path", type=Path)
    restore_parser.add_argument("--target-url", required=True)

    verify_parser = sub.add_parser("verify", help="Verify a restored database against a backup.")
    verify_parser.add_argument("backup_run_id", type=int)
    verify_parser.add_argument("--target-url", required=True)

    sub.add_parser(
        "latest",
        help="Print the most recent successful backup as 'id<TAB>path', for scripting.",
    )

    args = parser.parse_args(argv)

    if args.command == "backup":
        result = create_backup()
        print(  # noqa: T201 - CLI output, not logging
            f"backup_run_id={result.backup_run_id} artifact={result.artifact_path} "
            f"size_bytes={result.size_bytes} sha256={result.sha256}"
        )
        return 0
    if args.command == "restore":
        restore_backup(args.encrypted_path, args.target_url)
        print(f"restored {args.encrypted_path} into target database")  # noqa: T201
        return 0
    if args.command == "verify":
        result = verify_restore(args.backup_run_id, args.target_url)
        print(f"verification_status={result.status} details={result.details}")  # noqa: T201
        return 0 if result.status == "success" else 1
    if args.command == "latest":
        latest = latest_successful_backup()
        if latest is None:
            return 1
        print(f"{latest.backup_run_id}\t{latest.artifact_path}")  # noqa: T201
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
