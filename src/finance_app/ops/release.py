"""Release bookkeeping (handoff §19, ADR-008).

Pure database operations over `ops.releases` — no Docker/subprocess calls
live here. `finance_app.cli.finops`'s `deploy`/`rollback`/`restart`
commands call into this module for state and separately shell out to
`docker compose` for the actual container operation, so the state
transitions here stay unit-testable without a real container runtime.

Model, per ADR-008 ("track both current and previous known-good release
so rollback is one command"):

    at most one row with status == "current"
    at most one row with status == "previous"
    any number of "failed" / "rolled_back" rows (history)

`start_deploy` -> `mark_healthy` is the success path. `start_deploy` ->
`mark_failed` is the auto-rollback trigger path (handoff §14 step 12,
ADR-008's "post-deploy health checks trigger automatic rollback").
`rollback` promotes the tracked previous release back to current without
needing a new image build or registry fetch.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.db.models.ops import Release


class NoPreviousReleaseError(RuntimeError):
    """`finops rollback` was called but no previous known-good release is
    tracked — e.g. this is the first-ever deploy. Nothing to roll back to;
    the operator must fix forward instead."""


def get_current(session: Session) -> Release | None:
    return (
        session.execute(
            select(Release).where(Release.status == "current").order_by(Release.deployed_at.desc())
        )
        .scalars()
        .first()
    )


def get_previous(session: Session) -> Release | None:
    return (
        session.execute(
            select(Release).where(Release.status == "previous").order_by(Release.deployed_at.desc())
        )
        .scalars()
        .first()
    )


def start_deploy(session: Session, *, release_id: str, image_ref: str) -> Release:
    """Record a new deploy attempt. Does not touch the existing current/
    previous rows yet — that only happens once the new release is
    confirmed healthy (`mark_healthy`) or confirmed failed (`mark_failed`),
    so a crash mid-deploy never leaves the tracked state pointing at a
    release that was never actually verified."""
    release = Release(release_id=release_id, image_ref=image_ref, status="deploying")
    session.add(release)
    session.flush()
    return release


def mark_healthy(session: Session, release: Release) -> None:
    """Promote `release` to current. The prior current release (if any)
    becomes the tracked previous release; whatever was previous before
    that is left as plain history — ADR-008 only requires *one* rollback
    step to be trivial, not an arbitrary-depth undo stack."""
    old_current = get_current(session)
    old_previous = get_previous(session)
    if old_previous is not None and old_previous.id != (old_current.id if old_current else None):
        old_previous.status = "history"
    if old_current is not None:
        old_current.status = "previous"
    release.status = "current"
    release.health_check_status = "healthy"


def mark_failed(session: Session, release: Release, *, reason: str) -> None:
    release.status = "failed"
    release.health_check_status = "unhealthy"
    release.notes = reason[:2000]


def rollback(session: Session) -> Release:
    """Promote the tracked previous release back to current; demote and
    mark the (unhealthy) current release `rolled_back`. Raises
    `NoPreviousReleaseError` if there is nothing to roll back to."""
    previous = get_previous(session)
    if previous is None:
        raise NoPreviousReleaseError("No previous known-good release is tracked.")
    current = get_current(session)
    if current is not None:
        current.status = "rolled_back"
        current.rolled_back_at = datetime.datetime.now(datetime.UTC)
    previous.status = "current"
    return previous
