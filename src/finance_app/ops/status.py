"""Status-gathering logic behind every `finops` read command (handoff §10).

Each function here returns a small, JSON-serializable dict — no ORM
objects, no financial payloads, nothing that isn't already safe to print
to a terminal or emit as machine-readable output. `finance_app.cli.finops`
is a thin Typer/Rich presentation layer over these functions; keeping the
gathering logic here (rather than inline in the CLI module) makes it unit
testable without invoking Typer, and reusable from `finops health`'s
aggregate view.

Every query here reads through `finance_observer` (`ops.*`, read-only) or,
where sync/plaid metadata is needed, connects with the same read-only
posture — never `finance_owner`/`finance_migrator`. See
docs/security-model.md invariant 5 and CLAUDE.md's tool-boundary rule,
which applies to `finops` diagnosis just as much as to the runtime agent:
narrow, semantic, auditable — not a `psql` prompt.
"""

import datetime
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError as SASQLOperationalError
from sqlalchemy.orm import Session

from finance_app import __version__
from finance_app.config.settings import Settings, get_settings
from finance_app.db.models.ops import BackupRun, OperationalError, Release, SyncRun
from finance_app.db.models.plaid import Item, SyncState

# A sync more than this many hours stale is flagged unhealthy — the daily
# timer's mandatory cadence (ADR-011/ADR-012) means anything beyond ~36h
# (a day plus slack for a retried/late run) indicates the timer itself is
# broken, not just a slow API call.
_STALE_SYNC_HOURS = 36
# A backup with no successful restore verification in this many days is
# flagged unhealthy — "a backup that has never been restored is not
# verified" (handoff §22, ADR-015), and a stale verification is nearly as
# uninformative as none.
_STALE_BACKUP_VERIFICATION_DAYS = 14


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def version_info(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "app_version": __version__,
        "release_id": settings.release_id or None,
    }


def db_status(session: Session) -> dict[str, Any]:
    """Basic connectivity/reachability — not a data-content check."""
    try:
        session.execute(select(1))
        return {"status": "healthy", "detail": None}
    except SASQLOperationalError as exc:
        return {"status": "unreachable", "detail": type(exc).__name__}


def migration_status(session: Session, alembic_ini_path: str = "alembic.ini") -> dict[str, Any]:
    """Compares the DB's applied Alembic version against the repo's head
    revision. A mismatch means the running image and the database schema
    have drifted — exactly the situation ADR-008's "migration preflight"
    release step exists to prevent from ever reaching production."""
    try:
        applied = _current_alembic_version(session)
    except SASQLOperationalError as exc:
        return {"status": "unreachable", "detail": type(exc).__name__}

    try:
        config = Config(alembic_ini_path)
        script = ScriptDirectory.from_config(config)
        head = script.get_current_head()
    except Exception:  # noqa: BLE001 - repo layout may be unavailable (e.g. inside a
        # container that doesn't ship migrations/); degrade to "unknown" rather
        # than fail the whole status call.
        head = None

    if applied is None:
        return {"status": "unmigrated", "applied": None, "head": head}
    if head is None:
        return {"status": "unknown", "applied": applied, "head": None}
    return {
        "status": "up_to_date" if applied == head else "drift",
        "applied": applied,
        "head": head,
    }


def _current_alembic_version(session: Session) -> str | None:
    """The DB's applied migration head, read directly from
    `alembic_version` — the same table `alembic current` reads. A plain
    `text()` query rather than reflection: this is a one-column read of a
    table whose shape Alembic itself owns and never changes."""
    row = session.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def sync_status(session: Session) -> dict[str, Any]:
    latest_run = session.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    latest_state = session.execute(
        select(SyncState).order_by(SyncState.id.desc()).limit(1)
    ).scalar_one_or_none()
    item = session.execute(select(Item).order_by(Item.id.desc()).limit(1)).scalar_one_or_none()

    if latest_run is None:
        return {"status": "never_run", "last_run": None, "item_status": None}

    stale = False
    if latest_run.status == "success" and latest_run.finished_at is not None:
        age_hours = (_utcnow() - latest_run.finished_at).total_seconds() / 3600
        stale = age_hours > _STALE_SYNC_HOURS

    return {
        "status": "stale" if stale else latest_run.status,
        "last_run": {
            "id": latest_run.id,
            "run_type": latest_run.run_type,
            "status": latest_run.status,
            "started_at": latest_run.started_at.isoformat(),
            "finished_at": latest_run.finished_at.isoformat() if latest_run.finished_at else None,
            "added_count": latest_run.added_count,
            "modified_count": latest_run.modified_count,
            "removed_count": latest_run.removed_count,
            "last_error": latest_run.last_error,
        },
        "cursor_present": bool(latest_state and latest_state.cursor),
        "item_status": item.status if item else None,
    }


def backup_status(session: Session) -> dict[str, Any]:
    latest_backup = session.execute(
        select(BackupRun)
        .where(BackupRun.run_type == "backup")
        .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest_backup is None:
        return {"status": "never_run", "last_backup": None, "last_verification": None}

    latest_verification = session.execute(
        select(BackupRun)
        .where(
            BackupRun.run_type == "restore_verification",
            BackupRun.verifies_backup_id == latest_backup.id,
        )
        .order_by(BackupRun.started_at.desc(), BackupRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    verified = False
    if latest_verification is not None and latest_verification.status == "success":
        verified = True
        if latest_verification.finished_at is not None:
            age_days = (_utcnow() - latest_verification.finished_at).total_seconds() / 86400
            verified = age_days <= _STALE_BACKUP_VERIFICATION_DAYS

    return {
        "status": "verified" if verified else "unverified",
        "last_backup": {
            "id": latest_backup.id,
            "status": latest_backup.status,
            "started_at": latest_backup.started_at.isoformat(),
            "finished_at": latest_backup.finished_at.isoformat()
            if latest_backup.finished_at
            else None,
            "size_bytes": latest_backup.size_bytes,
            "sha256": latest_backup.sha256,
        },
        "last_verification": {
            "id": latest_verification.id,
            "status": latest_verification.status,
            "started_at": latest_verification.started_at.isoformat(),
            "finished_at": latest_verification.finished_at.isoformat()
            if latest_verification.finished_at
            else None,
        }
        if latest_verification
        else None,
    }


def recent_errors(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = session.execute(
        select(OperationalError)
        .order_by(OperationalError.occurred_at.desc(), OperationalError.id.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": row.id,
            "occurred_at": row.occurred_at.isoformat(),
            "category": row.category,
            "message": row.message,
            "context": row.context,
        }
        for row in rows
    ]


def current_release(session: Session) -> dict[str, Any] | None:
    row = (
        session.execute(
            select(Release)
            .where(Release.status == "current")
            .order_by(Release.deployed_at.desc(), Release.id.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    return {
        "release_id": row.release_id,
        "image_ref": row.image_ref,
        "deployed_at": row.deployed_at.isoformat(),
        "health_check_status": row.health_check_status,
    }


def previous_release(session: Session) -> dict[str, Any] | None:
    row = (
        session.execute(
            select(Release)
            .where(Release.status == "previous")
            .order_by(Release.deployed_at.desc(), Release.id.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    return {
        "release_id": row.release_id,
        "image_ref": row.image_ref,
        "deployed_at": row.deployed_at.isoformat(),
    }


def aggregate_health(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    """The single `finops health` view: is the application, database,
    sync, and backup posture all in an expected state? This is the
    command health checks (deploy verification, `finance-health.timer`,
    an owner glancing at the system) should reach for first."""
    settings = settings or get_settings()
    db = db_status(session)
    migration = migration_status(session) if db["status"] == "healthy" else {"status": "unknown"}
    sync = sync_status(session) if db["status"] == "healthy" else {"status": "unknown"}
    backup = backup_status(session) if db["status"] == "healthy" else {"status": "unknown"}
    release = current_release(session) if db["status"] == "healthy" else None

    unhealthy_conditions = [
        db["status"] != "healthy",
        migration["status"] not in ("up_to_date",),
        sync["status"] in ("error", "stale"),
    ]
    overall = "unhealthy" if any(unhealthy_conditions) else "healthy"

    return {
        "application": "healthy",  # process is running to answer this at all
        "database": db["status"],
        "migrations": migration["status"],
        "sync": sync["status"],
        "backup": backup["status"],
        "release": release["release_id"] if release else settings.release_id or None,
        "overall": overall,
    }
