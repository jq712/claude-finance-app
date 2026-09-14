"""Release bookkeeping (handoff §19, ADR-008, revised for ADR-019's
bare-metal deploy model).

Pure database operations over `ops.releases` — no filesystem/subprocess
calls live here. `finance_app.cli.finops`'s `deploy`/`rollback`/`restart`
commands call into this module for state and separately shell out to the
release directory's own venv binary / `systemctl` (`ops/host.py`) for the
actual host operation, so the state transitions here stay unit-testable
without a real production host.

Model, per ADR-008 ("track both current and previous known-good release
so rollback is one command"), enforced by `migrations/versions/
0005_..._release_rollback_safety.py`'s partial unique index (QA-3 —
`ops.releases (status) WHERE status IN ('current', 'previous')`):

    at most one row with status == "current"
    at most one row with status == "previous"
    any number of "failed" / "rolled_back" / "history" rows (history)

`start_deploy` -> `mark_healthy` is the success path. `start_deploy` ->
`mark_failed` is the auto-rollback trigger path (handoff §14 step 12,
ADR-008's "post-deploy health checks trigger automatic rollback").
`mark_failed` deliberately never touches the current/previous rows: a
failed deploy attempt was never promoted, so whatever was `current`
before it started is still the release actually running. `rollback`
promotes the correct rollback target back to `current` without needing a
new image build or registry fetch — see `get_previous`'s docstring for
exactly how that target is resolved (QA-2).

Concurrency (QA-3): `mark_healthy` takes a non-blocking Postgres advisory
transaction lock (`pg_try_advisory_xact_lock`) before promoting a release
to `current`. `finops deploy` never holds a single DB transaction across
the real `docker compose pull/up` step (that would hold the lock for
however long an image pull takes), so this is a best-effort guard for the
genuinely dangerous window — two `mark_healthy` calls racing to decide who
becomes `current` — backed by the partial unique index as the actual
invariant enforcement.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finance_app.db.models.ops import Release

# Arbitrary, stable key for the single "who gets to promote a release to
# current" lock — there is exactly one production deploy target, so one
# fixed key is sufficient (no need to derive one per release/environment).
_DEPLOY_PROMOTION_LOCK_KEY = 771_100_501

# A deploy attempt left `pending` longer than this is certainly orphaned
# (QA-4) — a real deploy resolves to `current`/`failed` within a couple of
# minutes at most. Reaped as `failed` the next time a deploy starts, so a
# crashed `finops deploy` never leaves a permanently stuck row that a
# later rollback could mistake for something to preserve.
_STALE_PENDING_DEPLOY_MINUTES = 15


class NoPreviousReleaseError(RuntimeError):
    """`finops rollback` was called but no previous known-good release is
    tracked — e.g. this is the first-ever deploy. Nothing to roll back to;
    the operator must fix forward instead."""


class RollbackContendedError(RuntimeError):
    """`rollback` could not safely promote its target (QA-47): either the
    deploy-promotion advisory lock (QA-3) is held by a concurrent
    `mark_healthy`, or `current` no longer matches what the caller
    resolved and health-verified before calling this — a concurrent
    deploy or rollback landed during the probe window (a `docker compose
    run` of the release image: seconds to minutes). Promoting anyway
    would silently discard whatever that concurrent change was. Refuse
    and let the caller retry once things settle."""


def get_current(session: Session) -> Release | None:
    return (
        session.execute(
            select(Release)
            .where(Release.status == "current")
            .order_by(Release.deployed_at.desc(), Release.id.desc())
        )
        .scalars()
        .first()
    )


def _tracked_previous(session: Session) -> Release | None:
    """The raw `status == 'previous'` bookkeeping row, if any. Used
    internally by `mark_healthy`'s history cascade. Not the same thing as
    the public `get_previous`, which additionally resolves what a
    rollback should target after a failed deploy attempt — see that
    function's docstring."""
    return (
        session.execute(
            select(Release)
            .where(Release.status == "previous")
            .order_by(Release.deployed_at.desc(), Release.id.desc())
        )
        .scalars()
        .first()
    )


def get_previous(session: Session) -> Release | None:
    """The release a rollback right now should target (ADR-008).

    Three cases collapse into this one function:

    - The most recent deploy attempt is healthy/`current`: the rollback
      target is the tracked `previous` release — plain bookkeeping,
      populated by `mark_healthy`.
    - The most recent deploy attempt `failed`, or is still `pending`
      (QA-42 — a deploy killed between `start_deploy` and
      `mark_healthy`/`mark_failed`, e.g. SIGKILL, VPS reboot, OOM, or any
      exception the deploy path doesn't catch, before its row ever
      resolves): `mark_failed` never touches current/previous (see its
      docstring) and a `pending` attempt hasn't touched them either, so
      whatever is tracked as `current` right now is still the release
      that is actually running, whether or not the unresolved attempt's
      own container ever started — an operator or the deploy
      auto-rollback needs to redeploy *that* image, not skip past it to
      something older (QA-2, reached a second way by QA-42). If that
      `current` release has no deploy history of its own
      (`replaces_release_id is None` — it was the very first release ever
      deployed), there genuinely is nothing to fall back to, and this
      returns `None`. `_reap_stale_pending_deploys` (in `start_deploy`)
      eventually turns a `pending` row into `failed`, but only on the
      *next* deploy — this covers the rollback path in the meantime,
      which never calls it.
    """
    latest = (
        session.execute(select(Release).order_by(Release.deployed_at.desc(), Release.id.desc()))
        .scalars()
        .first()
    )
    if latest is not None and latest.status in ("failed", "pending"):
        current = get_current(session)
        if current is not None and current.replaces_release_id is not None:
            return current
        return None
    return _tracked_previous(session)


def _reap_stale_pending_deploys(session: Session) -> None:
    """A deploy killed between `start_deploy` and `mark_healthy`/
    `mark_failed` leaves its row stuck `pending` with nothing to ever
    resolve it (QA-4). Since a real deploy resolves within minutes, any
    `pending` row older than `_STALE_PENDING_DEPLOY_MINUTES` is certainly
    orphaned from a crashed process — reap it as `failed` so it stops
    silently occupying the deploy-in-progress state and a later rollback
    never mistakes it for something to preserve."""
    session.execute(
        text(
            "UPDATE ops.releases SET status = 'failed', "
            "health_check_status = 'unhealthy', "
            "notes = 'reaped: orphaned pending deploy attempt (process likely crashed)' "
            "WHERE status = 'pending' "
            "AND deployed_at < now() - make_interval(mins => :minutes)"
        ),
        {"minutes": _STALE_PENDING_DEPLOY_MINUTES},
    )


def start_deploy(session: Session, *, release_id: str, artifact_ref: str) -> Release:
    """Record a new deploy attempt. Does not touch the existing current/
    previous rows yet — that only happens once the new release is
    confirmed healthy (`mark_healthy`) or confirmed failed (`mark_failed`),
    so a crash mid-deploy never leaves the tracked state pointing at a
    release that was never actually verified.

    `artifact_ref` is a release directory path under ADR-019's bare-metal
    model (a GHCR image ref under the superseded Docker model) — this
    function's own logic has no opinion on which; it just records
    whatever the caller passes.

    Captures `replaces_release_id` — whatever is `current` right now —
    so a later failed-deploy rollback can target it directly (QA-2)."""
    _reap_stale_pending_deploys(session)
    replaces = get_current(session)
    release = Release(
        release_id=release_id,
        artifact_ref=artifact_ref,
        status="pending",
        replaces_release_id=replaces.release_id if replaces is not None else None,
    )
    session.add(release)
    session.flush()
    return release


def _try_acquire_promotion_lock(session: Session) -> bool:
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": _DEPLOY_PROMOTION_LOCK_KEY},
        ).scalar()
    )


def mark_healthy(session: Session, release: Release) -> None:
    """Promote `release` to current. The prior current release (if any)
    becomes the tracked previous release; whatever was previous before
    that is left as plain history — ADR-008 only requires *one* rollback
    step to be trivial, not an arbitrary-depth undo stack.

    Redeploying the SHA that is already current (an idempotent retry) is
    handled specially (QA-5): the old `current` row becomes `history`
    directly rather than `previous`, so `previous` never ends up holding
    the same `release_id` as `current` — which would make `finops
    rollback` a silent no-op that redeploys the identical image.

    Concurrency (QA-3): if another `mark_healthy` call is concurrently
    promoting a release (holds the deploy-promotion advisory lock), this
    call does not attempt to become `current` too — it records itself as
    `failed` instead. Its own image was never actually the one promoted,
    so that is an accurate outcome, not just a defensive one."""
    if not _try_acquire_promotion_lock(session):
        release.status = "failed"
        release.health_check_status = "unhealthy"
        release.notes = "concurrent deploy detected while promoting to current"
        return

    old_current = get_current(session)
    old_previous = _tracked_previous(session)
    same_sha_redeploy = old_current is not None and old_current.release_id == release.release_id

    if same_sha_redeploy:
        assert old_current is not None
        old_current.status = "history"
    else:
        if old_previous is not None and (old_current is None or old_previous.id != old_current.id):
            old_previous.status = "history"
        if old_current is not None:
            old_current.status = "previous"

    # Flush the demotion(s) before promoting `release` to `current`: the
    # partial unique index (migrations/versions/0005) is a plain index,
    # not a deferrable constraint (Postgres has no deferrable *partial*
    # unique constraint), so it is checked per-statement — the old
    # `current` row must already be demoted, in the database, before this
    # transaction's own UPDATE tries to give a second row `status =
    # 'current'`.
    session.flush()
    release.status = "current"
    release.health_check_status = "healthy"


def mark_failed(session: Session, release: Release, *, reason: str) -> None:
    release.status = "failed"
    release.health_check_status = "unhealthy"
    release.notes = reason[:2000]


def rollback(
    session: Session, *, release_row_id: int, expected_current_row_id: int | None = None
) -> Release:
    """Promote the release identified by `release_row_id` (the `ops.releases.id`
    primary key) to `current`.

    QA-47: `mark_healthy` is not the only writer of `status = 'current'` —
    this function is the other one, and until now it took none of
    `mark_healthy`'s own protection against a concurrent promotion (QA-3's
    `pg_try_advisory_xact_lock`). Two gaps, closed together:

    - **Same-instant race**: this now takes the identical advisory lock
      `mark_healthy` does before touching anything, so the two can never
      both be mid-promotion at once. Raises `RollbackContendedError`
      rather than racing if the lock is held.
    - **Sequential race across the probe window**: the lock alone does not
      catch a `mark_healthy` that *already completed* (acquired, promoted,
      committed, released the lock) between when the caller resolved this
      rollback's target and when it calls this function — exactly the gap
      `probe_release` opens (a `docker compose run`: seconds to minutes),
      and exactly what QA-41's row-identity fix did not address (it fixed
      *which row* gets promoted, not whether `current` is still what it
      was when that row was chosen). `expected_current_row_id`, when
      given, is compared against `get_current(session)` freshly read
      here; a mismatch means something changed `current` since the caller
      last looked, and this refuses rather than demoting a release it
      never probed and that may have just passed its own health check.
      `None` skips the check (the promotion lock alone still applies).

    QA-41: takes the target *row* directly rather than re-deriving it via
    `get_previous` — the caller (`_do_rollback` in `cli.finops`) already
    resolved and health-verified one specific release via `probe_release`
    before calling this, in a separate DB session. Calling `get_previous`
    again here re-runs that same, state-dependent resolution a second
    time; if `ops.releases` changed during the probe window (an image
    pull and container start: seconds to minutes — e.g. a concurrent
    deploy landed and failed), the second resolution can silently
    disagree with the first, promoting a release that was never actually
    probed while the CLI still reports the one that was.

    Keyed by the primary key rather than `release_id` (the Git SHA) —
    `ops.releases.release_id` is deliberately **not** unique
    (`migrations/versions/0004`'s index on it is `unique=False`;
    `mark_healthy`'s `same_sha_redeploy` handling exists precisely because
    a SHA can be redeployed and so recur across multiple rows, e.g. one
    `failed` attempt and one later `current` one). Resolving by
    `release_id` with an unordered `.first()` could silently promote an
    older *failed* attempt at the same SHA instead of the row that was
    actually probed. Resolve once, probe that row's release id, promote
    that exact row — never re-derive by a value that doesn't uniquely
    identify it.

    If the target row is already `current` (QA-2's failed-deploy/QA-42's
    pending-deploy case — neither `mark_failed` nor an unresolved
    `pending` row ever demoted it), this is a no-op at the bookkeeping
    level: nothing to promote or demote, it's already the right release.
    The caller still needs to redeploy its image via `docker compose up`,
    since the failed/interrupted attempt's container may already be
    running. Otherwise (a plain healthy-to-healthy rollback), the current
    release is demoted and marked `rolled_back` and the target is
    promoted, as before.

    Raises `NoPreviousReleaseError` if `release_row_id` no longer
    identifies any tracked release (it was resolved by the caller but has
    since been removed — not expected in practice, since rows here are
    never deleted, but this must not silently promote nothing).
    """
    target = session.get(Release, release_row_id)
    if target is None:
        raise NoPreviousReleaseError(
            f"release row {release_row_id!r} is no longer tracked in ops.releases."
        )
    if not _try_acquire_promotion_lock(session):
        raise RollbackContendedError(
            "a concurrent deploy is promoting a release right now; refusing to roll back "
            "until it finishes — retry once it settles."
        )
    current = get_current(session)
    if expected_current_row_id is not None:
        observed_current_row_id = current.id if current is not None else None
        if observed_current_row_id != expected_current_row_id:
            raise RollbackContendedError(
                "the current release changed since this rollback resolved and health-"
                f"verified its target (expected current row {expected_current_row_id!r}, "
                f"found {observed_current_row_id!r}) — a concurrent deploy or rollback "
                "landed during the probe; refusing to promote over it. Retry."
            )
    if current is not None and current.id == target.id:
        return target
    if current is not None:
        current.status = "rolled_back"
        current.rolled_back_at = datetime.datetime.now(datetime.UTC)
        # Same ordering reason as `mark_healthy`: demote before promoting,
        # in separate statements, so the partial unique index never sees
        # two `current` rows even transiently.
        session.flush()
    target.status = "current"
    return target
