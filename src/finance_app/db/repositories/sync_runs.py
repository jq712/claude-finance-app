"""Deterministic writes to `ops.sync_runs` — one audit row per sync
invocation, read by `finance_observer` for `finops sync-status` (handoff
§10, §23). Never holds financial payloads, only counts and identifiers."""

import datetime

from sqlalchemy.orm import Session

from finance_app.db.models.ops import SyncRun


def start(session: Session, *, item_id: int, run_type: str) -> SyncRun:
    run = SyncRun(item_id=item_id, run_type=run_type, status="running")
    session.add(run)
    session.flush()
    return run


def finish_success(
    session: Session,
    run: SyncRun,
    *,
    added_count: int,
    modified_count: int,
    removed_count: int,
    request_id: str | None,
) -> None:
    run.status = "success"
    run.finished_at = datetime.datetime.now(datetime.UTC)
    run.added_count = added_count
    run.modified_count = modified_count
    run.removed_count = removed_count
    run.last_request_id = request_id


def finish_failure(session: Session, run: SyncRun, *, error: str) -> None:
    run.status = "error"
    run.finished_at = datetime.datetime.now(datetime.UTC)
    run.last_error = error[:2000]
