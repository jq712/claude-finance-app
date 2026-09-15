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
import json
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError as SASQLOperationalError
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from finance_app import __version__
from finance_app.config.settings import Settings, get_settings
from finance_app.db.models.ops import BackupRun, OperationalError, Release, SyncRun
from finance_app.db.models.plaid import Item, SyncState
from finance_app.ops.host import (
    DEFAULT_RELEASE_ROOT,
    HostCommandError,
    current_link,
    read_current_target,
    release_dir,
)
from finance_app.ops.host import run_release as _run_release

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
        # A failed statement leaves the transaction deactivated (Postgres
        # aborts it on any error) until it's rolled back — the same
        # reason `migration_status` below rolls back its own
        # `ProgrammingError`. Without this, the *caller's* next statement
        # on this session raises `PendingRollbackError` instead of
        # whatever it was actually trying to do — reachable here because
        # `aggregate_health`/`deploy_health_check` keep using this session
        # after calling `db_status`, and `observer_session_scope`'s own
        # `session.commit()` on a clean exit would otherwise be the thing
        # that raises, past any caller that already handled the original
        # "unreachable" result.
        session.rollback()
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
    except ProgrammingError:
        # `alembic_version` itself doesn't exist yet -- a database that has
        # never had `alembic upgrade head` run against it, e.g. the state
        # of the VPS during the very first `finops deploy` (QA-6). Postgres
        # aborts the current transaction on a failed statement, so roll
        # back before this session is used for anything else (aggregate_health
        # runs further queries on the same session).
        session.rollback()
        applied = None

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
        "artifact_ref": row.artifact_ref,
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
        "artifact_ref": row.artifact_ref,
        "deployed_at": row.deployed_at.isoformat(),
    }


def release_topology(
    session: Session, *, release_root: str = DEFAULT_RELEASE_ROOT
) -> dict[str, Any]:
    """Compares what `ops.releases` bookkeeping believes is `current`
    against what the `current` symlink actually resolves to on disk
    (ADR-019). This is the bare-metal analogue of "which image is
    actually running" — a failure mode the Docker model never had,
    because a container's identity couldn't drift from what `docker
    compose` last started without a corresponding record. A symlink can:
    a hand-repointed `current`, an interrupted rollback, or a crash
    between `repoint_current` and the bookkeeping commit all leave the
    two disagreeing. `finops restart` refuses to proceed on a
    disagreement rather than silently restarting whatever `current`
    happens to point at."""
    bookkeeping = current_release(session)
    symlink_target = read_current_target(release_root)
    bookkeeping_release_id = bookkeeping["release_id"] if bookkeeping is not None else None
    return {
        "bookkeeping_current": bookkeeping_release_id,
        "symlink_current": symlink_target,
        "agrees": bookkeeping_release_id == symlink_target,
    }


def _application_liveness() -> str:
    """A lightweight in-process liveness signal for `aggregate_health`
    (QA-8): this function running at all proves the process's import
    graph and settings came up, which a crash-looping process never gets
    to. Deliberately *not* the same check as `deploy_health_check`'s
    `probe_release` below — that one is externally observed (a one-shot
    `finance selfcheck` run of the exact release directory under
    deployment) and so can actually catch "the release itself is
    broken", not just "this already-running process still imports". This
    one runs from *inside* the process being asked "are you healthy" and
    so cannot catch that failure mode — it is a weaker signal, used here
    only because `aggregate_health` is also called from `finops health`/
    `finance-health.timer`, which run inside the plain observer context
    with no reason to shell out to a release binary. Still strictly
    better than a literal that could never fail."""
    try:
        get_settings()
    except Exception:  # noqa: BLE001 - any failure here means "not healthy", full stop
        return "unhealthy"
    return "healthy"


def _parse_selfcheck_stdout(stdout: str) -> dict[str, Any] | None:
    """The line of `finance selfcheck --json`'s stdout that is its actual
    payload — a JSON object carrying both `release_id` and `overall` — or
    `None` if there isn't one.

    Scans the whole stream and keeps going past any line that doesn't fit,
    rather than stopping at the first one: a plain venv console-script
    still shares stdout with `configure_logging`'s JSON handler, and
    nothing rules out an unrelated JSON object (a structured log line —
    the process runs with `LOG_FORMAT: json`, so its own log stream
    already shares this file descriptor) elsewhere in the stream.
    Requiring both sentinel keys, rather than accepting the first
    parseable dict, keeps such a line from being misread as the selfcheck
    payload and reported as `wrong_release` or `unreachable` for a
    release that is actually fine.

    QA-36: every string on this stream is attacker-influenceable per
    CLAUDE.md (Plaid merchant text, model responses, `ops.errors.message`
    could all end up quoted into a log line), so when more than one
    candidate line matches the sentinel shape, there is no positional rule
    ("last one wins") that can be trusted to pick the real payload over a
    lookalike. If every candidate is identical, there is no real
    ambiguity (something printed the same line twice) and it is returned
    as-is. Otherwise this fails closed: a deterministic gate must not
    resolve "which of these is the real payload" by position, so
    `overall` is forced to a value `probe_release` never treats as
    healthy, and every disagreeing candidate is preserved in `detail` for
    the operator instead of silently discarded."""
    candidates: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "release_id" in payload and "overall" in payload:
            candidates.append(payload)
    if not candidates:
        return None
    first = candidates[0]
    if all(candidate == first for candidate in candidates[1:]):
        return first
    return {
        "release_id": first.get("release_id"),
        "installed_release_id": first.get("installed_release_id"),
        "overall": "ambiguous",
        "ambiguous_candidates": candidates,
    }


def probe_release(
    *,
    release_id: str,
    release_root: str = DEFAULT_RELEASE_ROOT,
    via_current: bool = False,
    run_release_fn: Any = _run_release,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """A one-shot run of `finance selfcheck --json` against a *specific*
    release tree (ADR-019, carrying forward ADR-016 D3's reasoning): the
    exact release directory under deployment, with `RELEASE_ID` pinned to
    `release_id` for reporting purposes only — never for the identity
    comparison below, which is exactly the tautology QA-37 was about.

    `via_current` selects which path is actually invoked:
    `<release_root>/releases/<release_id>/.venv/bin/finance` (the normal
    pre-promotion probe, before `current` has been touched) when `False`,
    or `<release_root>/current/.venv/bin/finance` (a post-restart
    liveness/identity re-check *through the symlink itself*) when `True`
    — this is what lets `finops restart`/the post-deploy re-probe detect
    "the process that actually started is not the release we just
    promoted", a failure mode unique to a mutable symlink that a
    container tag never had.

    Returns `{"status": "healthy"|"unhealthy"|"wrong_release"|"unreachable",
    "reported_release_id": str | None, "detail": ...}`. `"wrong_release"`
    — the release that actually ran reports an `installed_release_id`
    other than the one requested — is compared against
    `ops/identity.py`'s file-based identity, never against the
    `RELEASE_ID` environment variable this very call sets a few lines
    down (QA-37: comparing that against itself can never disagree, so it
    could never actually detect a wrong release)."""

    release_path = (
        current_link(release_root) if via_current else release_dir(release_root, release_id)
    )
    env = {"RELEASE_ID": release_id}
    try:
        result = run_release_fn(
            release_path,
            "finance",
            "selfcheck",
            "--json",
            env=env,
            timeout=timeout,
        )
    except HostCommandError as exc:
        payload = _parse_selfcheck_stdout(exc.stdout)
        if payload is None:
            return {"status": "unreachable", "reported_release_id": None, "detail": str(exc)}
        reported = payload.get("installed_release_id")
        status_value = "wrong_release" if reported != release_id else "unhealthy"
        return {"status": status_value, "reported_release_id": reported, "detail": payload}

    payload = _parse_selfcheck_stdout(result.stdout)
    if payload is None:
        return {
            "status": "unreachable",
            "reported_release_id": None,
            "detail": "selfcheck produced no parseable JSON output",
        }
    reported = payload.get("installed_release_id")
    if reported != release_id:
        return {"status": "wrong_release", "reported_release_id": reported, "detail": payload}
    if payload.get("overall") != "healthy":
        return {"status": "unhealthy", "reported_release_id": reported, "detail": payload}
    return {"status": "healthy", "reported_release_id": reported, "detail": payload}


def deploy_health_check(
    session: Session,
    *,
    release_id: str,
    release_root: str = DEFAULT_RELEASE_ROOT,
    via_current: bool = False,
    run_release_fn: Any = _run_release,
) -> dict[str, Any]:
    """The narrow health gate `finops deploy` uses to decide whether to
    promote a release to `current` or trigger ADR-008's auto-rollback —
    checking only things *this deploy* can actually break: is the
    database reachable, and does the release tree itself (`probe_release`)
    come up healthy at its own reported migration head. Deliberately does
    not fold in `aggregate_health`'s broader operational signals (Plaid
    sync staleness, backup verification) — QA-14: a pre-existing stale
    sync or unverified backup has nothing to do with whether the release
    that was *just* deployed is healthy, and conflating the two meant a
    pre-existing Plaid outage auto-rolled back every subsequent deploy —
    including the deploy of the fix for that very outage — discarding a
    known-good release each time (QA-2). See `aggregate_health` for the
    broader view `finops health`/the health timer use instead.

    Does not call `migration_status` itself (QA-22): this function's own
    result must not depend on which release directory the calling process
    happens to be running from — only the release tree under probe (via
    `probe_release`, from inside its own selfcheck) can answer what
    migration head *it* expects."""
    db = db_status(session)
    application = probe_release(
        release_id=release_id,
        release_root=release_root,
        via_current=via_current,
        run_release_fn=run_release_fn,
    )
    healthy = db["status"] == "healthy" and application["status"] == "healthy"
    detail = application["detail"]
    migrations_status = (
        detail.get("migrations", {}).get("status") if isinstance(detail, dict) else "unknown"
    )
    return {
        "overall": "healthy" if healthy else "unhealthy",
        "database": db["status"],
        "migrations": migrations_status,
        "application": application,
    }


def aggregate_health(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    """The single `finops health` view: is the application, database,
    sync, and backup posture all in an expected state? This is the
    command health checks (`finance-health.timer`, an owner glancing at
    the system) should reach for first. Not what `finops deploy` gates
    promotion/rollback on — see `deploy_health_check` (QA-14) for that
    narrower view."""
    settings = settings or get_settings()
    db = db_status(session)
    migration = migration_status(session) if db["status"] == "healthy" else {"status": "unknown"}
    # Every other table this function reads lives in a schema migrations
    # create — querying them against a database that is merely reachable
    # but not yet migrated (or whose migration state we couldn't
    # determine) raises `ProgrammingError`/`UndefinedTable` instead of
    # producing a status (QA-6's failure mode, one level up).
    schema_ready = db["status"] == "healthy" and migration["status"] not in (
        "unmigrated",
        "unreachable",
    )
    sync = sync_status(session) if schema_ready else {"status": "unknown"}
    backup = backup_status(session) if schema_ready else {"status": "unknown"}
    release = current_release(session) if schema_ready else None
    application = _application_liveness()

    unhealthy_conditions = [
        db["status"] != "healthy",
        migration["status"] not in ("up_to_date",),
        sync["status"] in ("error", "stale"),
        # QA-7: a permanently broken backup pipeline previously never
        # surfaced here, so it never generated an ops.errors row via
        # finance-health.timer either. "unverified" means a backup and/or
        # its restore-verification actually ran and did not succeed (or
        # is stale) -- that's a real regression and must gate overall
        # health. "never_run" is deliberately excluded: a fresh deploy
        # before the first daily backup timer fires is not yet unhealthy,
        # just not yet proven.
        backup["status"] == "unverified",
        application != "healthy",
    ]
    overall = "unhealthy" if any(unhealthy_conditions) else "healthy"

    return {
        "application": application,
        "database": db["status"],
        "migrations": migration["status"],
        "sync": sync["status"],
        "backup": backup["status"],
        "release": release["release_id"] if release else settings.release_id or None,
        "overall": overall,
    }
