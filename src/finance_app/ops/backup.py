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
import contextlib
import datetime
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select, text
from sqlalchemy.engine import create_engine, make_url

from finance_app.config.settings import Settings, get_settings
from finance_app.db.models.ops import BackupRun
from finance_app.db.session import session_scope

# Staging directories for plaintext dump/restore data live under
# `tempfile.gettempdir()` (`/tmp` in production), never inside `backup_dir`
# itself (QA-10) -- `tempfile.mkdtemp()` creates them mode 0700, and
# `/tmp` is typically cleared on reboot, which is the best available
# mitigation against a plaintext file surviving a SIGKILL/OOM-killed
# process (nothing can catch SIGKILL; a startup sweep plus a SIGTERM
# handler cover the rest -- see `_sweep_stale_staging_dirs`).
_STAGING_PREFIX = "finance-backup-"
_STALE_STAGING_MAX_AGE_SECONDS = 6 * 3600

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
    `pg_dump`/`pg_restore` (libpq) understand.

    Parses with SQLAlchemy's own `make_url` (which correctly splits
    `user:password@host` even when the password itself contains `/`,
    `@`, or `%` — a bare string/regex split does not, see QA-11) rather
    than a fixed string replace, then percent-encodes the username and
    password before rebuilding the URI. This matters because
    `docs/runbooks/deploy.md` prescribes `openssl rand -base64 32` for
    every database role password, and base64 output contains `/` about
    48% of the time — unescaped, that breaks libpq's URI parser (`pg_dump`
    fails with a confusing "invalid integer value ... for connection
    option \"port\"" instead of an authentication error)."""
    url = make_url(sqlalchemy_url)
    user = quote(url.username or "", safe="")
    password = f":{quote(url.password or '', safe='')}" if url.password is not None else ""
    host = url.host or ""
    port = f":{url.port}" if url.port else ""
    database = url.database or ""
    return f"postgresql://{user}{password}@{host}{port}/{database}"


def _pg_connection_args(sqlalchemy_url: str) -> tuple[list[str], dict[str, str]]:
    """Host/port/user/dbname-only argv pieces for `pg_dump`/`pg_restore`,
    plus a `PGPASSWORD` env override carrying the credential instead
    (finding 2, handoff §22). The previous implementation passed the full
    `--dbname=postgresql://user:password@host/db` DSN as a CLI argument —
    visible in `ps`/`/proc/<pid>/cmdline` for the entire duration of the
    dump/restore, on any multi-user host. Neither `ps` nor
    `/proc/<pid>/cmdline` exposes a subprocess's environment to other
    users the way argv is exposed."""
    url = make_url(sqlalchemy_url)
    args = [f"--host={url.host or ''}"]
    if url.port:
        args.append(f"--port={url.port}")
    if url.username:
        args.append(f"--username={url.username}")
    args.append(f"--dbname={url.database or ''}")
    return args, {"PGPASSWORD": url.password or ""}


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Run a subprocess, raising `BackupError` with a sanitized message on
    failure. `command` must never contain a secret in argv — passphrases
    are always passed via stdin (`input_text`) and database passwords via
    `env` (`PGPASSWORD`, see `_pg_connection_args`), so neither ever
    appears in `ps`, shell history, or an error message built from
    `command`. `env`, when given, is merged onto the current process
    environment (not substituted for it — the same env-replacement bug
    class as QA-1 in `ops/compose.py`) so `PATH` and everything else
    `pg_dump`/`gpg` need to run at all still reaches the child."""
    try:
        subprocess.run(  # noqa: S603 - fixed argv built by this module, never from user input
            command,
            input=input_text,
            text=True,
            check=True,
            capture_output=True,
            env={**os.environ, **env} if env is not None else None,
        )
    except subprocess.CalledProcessError as exc:
        # stderr from pg_dump/gpg can be verbose but is not expected to
        # contain secrets (they're passed via stdin/env, not argv/output);
        # still cap length defensively before it reaches ops.backup_runs.error.
        raise BackupError(
            f"{command[0]} failed (exit {exc.returncode}): {exc.stderr[:500]}"
        ) from exc


def _sweep_stale_staging_dirs() -> None:
    """Remove leftover plaintext staging directories from a previous
    `create_backup`/`restore_backup` that was SIGKILLed/OOM-killed/timed
    out before its own `finally: shutil.rmtree(...)` ran (QA-10). Nothing
    can catch SIGKILL, so this startup sweep plus staging under `/tmp`
    (typically cleared on reboot anyway) is the practical mitigation, on
    top of the SIGTERM handler in `_sigterm_as_backup_error` catching the
    common "systemd hit TimeoutStartSec" case."""
    base = Path(tempfile.gettempdir())
    now = time.time()
    for path in base.glob(f"{_STAGING_PREFIX}*"):
        try:
            age = now - path.stat().st_mtime
            if age > _STALE_STAGING_MAX_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _new_staging_dir() -> Path:
    """A private (`tempfile.mkdtemp` default mode 0700), never-inside-
    `backup_dir` location for plaintext dump/restore data (QA-10) — the
    previous implementation staged plaintext directly in `backup_dir`
    (world-readable under the default umask, despite the module
    docstring's "private temp location" claim) and cleaned it up with a
    bare `finally: unlink`, which never runs on SIGKILL/OOM/a hard
    timeout."""
    _sweep_stale_staging_dirs()
    return Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX))


class _Interrupted(BackupError):
    """Raised from the SIGTERM handler installed by
    `_sigterm_as_backup_error` so an interrupted backup/restore is
    recorded as a normal `BackupError` failure (status='failed', staging
    directory cleaned up in the caller's `finally`) instead of the
    process just dying with the plaintext staging directory left behind."""


@contextlib.contextmanager
def _sigterm_as_backup_error(action: str):
    """Turn SIGTERM (what systemd sends on `TimeoutStartSec`/a manual
    `systemctl stop`) into a normal, catchable `BackupError` for the
    duration of the wrapped block, restoring the previous handler
    afterward. Does not help against SIGKILL (uncatchable by design) —
    see `_sweep_stale_staging_dirs` for that case."""

    def _handler(signum: int, frame: object) -> None:  # noqa: ARG001
        raise _Interrupted(f"{action} interrupted by SIGTERM")

    previous = signal.signal(signal.SIGTERM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _dump_with_consistent_snapshot(database_url: str, plaintext_path: Path) -> dict[str, int]:
    """Run `pg_dump` and capture the sanity row counts against the exact
    same database snapshot (QA-9): the previous implementation queried
    row counts on a separate connection *before* starting `pg_dump`, so
    any write landing in between made the counts describe a different
    point in time than the artifact actually contains — a false
    verification failure for a perfectly valid backup, on the one channel
    that is supposed to prove backups work.

    Opens a `REPEATABLE READ, READ ONLY` transaction, exports its
    snapshot with `pg_export_snapshot()`, passes that snapshot id to
    `pg_dump --snapshot=...` (so pg_dump reads under the *same* snapshot
    rather than starting its own), and runs the row-count queries inside
    that same transaction before it closes. An exported snapshot only
    exists while its exporting transaction is open, so the transaction is
    kept open for the duration of the `pg_dump` subprocess call."""
    engine = create_engine(database_url)
    try:
        conn = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        try:
            with conn.begin():
                snapshot_id = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                row_counts = {
                    table: conn.execute(
                        text(f"SELECT count(*) FROM {_quote_qualified(table)}")  # noqa: S608
                    ).scalar_one()
                    for table in SANITY_TABLES
                }
                pg_args, pg_env = _pg_connection_args(database_url)
                _run(
                    [
                        "pg_dump",
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        f"--snapshot={snapshot_id}",
                        f"--file={plaintext_path}",
                        *pg_args,
                    ],
                    env=pg_env,
                )
        finally:
            conn.close()
    finally:
        engine.dispose()
    return row_counts


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

    # 0700: the backup directory holds nothing plaintext (the artifact
    # inside it is GPG-encrypted), but there is no reason for it to be
    # group/other-readable either (QA-10). `mkdir(mode=...)` alone is not
    # enough -- it's still subject to the process umask -- so `chmod`
    # explicitly, including on an already-existing directory from a
    # previous run.
    output_dir = Path(settings.backup_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    encrypted_path = output_dir / f"finance-{timestamp}.dump.gpg"

    staging_dir = _new_staging_dir()
    plaintext_path = staging_dir / "finance.dump"

    with session_scope() as session:
        run = BackupRun(run_type="backup", status="running")
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        with _sigterm_as_backup_error("backup"):
            row_counts = _dump_with_consistent_snapshot(
                settings.backup_database_url.get_secret_value(), plaintext_path
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
        encrypted_path.chmod(0o600)
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
        shutil.rmtree(staging_dir, ignore_errors=True)


def restore_backup(
    encrypted_path: Path,
    target_database_url: str,
    settings: Settings | None = None,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Decrypt and `pg_restore` an encrypted backup into `target_database_url`.

    `target_database_url` must be a scratch database, never production —
    `pg_restore --clean` drops existing objects first. Callers (the
    restore-verification drill, an owner-performed disaster recovery)
    are responsible for pointing this at a database that is safe to
    overwrite; see docs/runbooks/restore-backup.md.

    `expected_sha256` — the `ops.backup_runs.sha256` recorded when the
    backup was created — is checked against the artifact on disk
    *before* decrypting it, if given. Recording the hash and never
    checking it was a real integrity gap (finding 7 / QA-19): a
    substituted or corrupted-in-place artifact would otherwise only be
    caught by GPG's own MDC (which detects corruption/tampering of the
    ciphertext, but only at decrypt time, and does not compare against
    the value `create_backup` recorded). `deploy/scripts/restore-verify.sh`
    (the automated weekly drill) always passes this; a manual disaster-
    recovery `restore.sh` invocation may not always have a `backup_run_id`
    on hand, so it remains optional here.
    """
    settings = settings or get_settings()
    passphrase = settings.backup_encryption_key.get_secret_value()
    if not passphrase:
        raise BackupError("BACKUP_ENCRYPTION_KEY is not set; cannot decrypt.")

    if expected_sha256 is not None:
        actual_sha256 = hashlib.sha256(encrypted_path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise BackupError(
                "recorded sha256 does not match the artifact on disk -- refusing to "
                "restore a backup that may have been substituted, corrupted, or "
                f"tampered with (expected {expected_sha256}, got {actual_sha256})"
            )

    staging_dir = _new_staging_dir()
    plaintext_path = staging_dir / "finance.restore"
    try:
        with _sigterm_as_backup_error("restore"):
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
            pg_args, pg_env = _pg_connection_args(target_database_url)
            _run(
                [
                    "pg_restore",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    *pg_args,
                    str(plaintext_path),
                ],
                env=pg_env,
            )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


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
    restore_parser.add_argument(
        "--backup-run-id",
        type=int,
        default=None,
        help=(
            "ops.backup_runs.id this artifact came from -- when given, its recorded "
            "sha256 is verified against the artifact on disk before decrypting "
            "(finding 7 / QA-19). deploy/scripts/restore-verify.sh always passes this."
        ),
    )

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
        expected_sha256 = None
        if args.backup_run_id is not None:
            with session_scope() as session:
                source = session.get(BackupRun, args.backup_run_id)
                if source is None:
                    print(  # noqa: T201
                        f"restore: no ops.backup_runs row with id={args.backup_run_id}",
                        file=sys.stderr,
                    )
                    return 1
                expected_sha256 = source.sha256
        restore_backup(args.encrypted_path, args.target_url, expected_sha256=expected_sha256)
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
