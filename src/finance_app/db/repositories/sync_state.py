"""Deterministic writes to `plaid.sync_state` — the durable cursor for the
single Item's Transactions Sync (handoff §6.1). The cursor here only ever
advances to a value that a committed database transaction has already
processed; see plaid/sync.py for the commit ordering that makes that true."""

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.db.models.plaid import SyncState


def get_or_create(session: Session, *, item_id: int) -> SyncState:
    state = session.execute(
        select(SyncState).where(SyncState.item_id == item_id)
    ).scalar_one_or_none()
    if state is None:
        state = SyncState(item_id=item_id, cursor=None, status="idle")
        session.add(state)
        session.flush()
    return state


def record_attempt(session: Session, state: SyncState) -> None:
    state.status = "running"
    state.last_attempted_at = datetime.datetime.now(datetime.UTC)


def advance(
    session: Session,
    state: SyncState,
    *,
    cursor: str,
    added_count: int,
    modified_count: int,
    removed_count: int,
    request_id: str | None,
) -> None:
    """Commit-time bookkeeping for one successfully processed page. Called
    inside the same transaction as the page's row writes, so the cursor
    only ever advances alongside the data it describes."""
    state.cursor = cursor
    state.added_count = added_count
    state.modified_count = modified_count
    state.removed_count = removed_count
    state.last_request_id = request_id


def record_success(session: Session, state: SyncState) -> None:
    state.status = "idle"
    state.last_successful_at = datetime.datetime.now(datetime.UTC)
    state.last_error = None


def record_failure(session: Session, state: SyncState, *, error: str) -> None:
    state.status = "error"
    state.last_error = error[:2000]
