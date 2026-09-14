"""Deterministic post-deploy smoke check (ADR-016 D3, adapted for ADR-019's
bare-metal release model).

Runs *inside* the release tree under deployment, never from the `finops`
control-plane process — that process may be running from a *different*
release directory (the previous `current`, or the dev tree), so it can
never report what the *new* release's migration head is or even confirm
which release actually started. Only the release tree itself can answer
that, which is why `finops deploy` shells out to `finance selfcheck --json`
(see `ops.status.probe_release`) rather than probing anything from its own
process.

Reads as `finance_app` (`db.session.session_scope`) — the same role, same
credential, every other application code path already holds. Never calls a
model provider and never needs a provider API key, so selfcheck stays
usable even when `AGENT_PROVIDER`'s credential is absent or wrong — that
failure belongs to `finance chat`, not to the deploy gate.
"""

from __future__ import annotations

from typing import Any

from finance_app import __version__
from finance_app.config.settings import get_settings
from finance_app.db.session import session_scope
from finance_app.ops.identity import installed_release_id
from finance_app.ops.status import db_status, migration_status


def selfcheck() -> dict[str, Any]:
    """{"release_id", "installed_release_id", "app_version", "database":
    {...}, "migrations": {...}, "overall": "healthy"|"unhealthy"}.
    `overall` is healthy only when the database is reachable *and* this
    release's migration head matches what's applied — the same bar
    `deploy_health_check` used to hold the deploy control plane to, now
    answered by the party that actually knows it. `installed_release_id`
    (carrying forward QA-37's invariant) is the identity read from the
    `RELEASE_ID` file inside the release tree this process is actually
    executing from (`ops/identity.py`) — fixed at `git archive` time,
    before this deploy ever ran — distinct from `release_id` (the
    `RELEASE_ID` *environment variable* this process happened to be
    started with). `probe_release`'s `wrong_release` check needs the
    former; the latter can never disagree with what `probe_release` itself
    just injected."""
    settings = get_settings()
    with session_scope() as session:
        database = db_status(session)
        migrations = (
            migration_status(session)
            if database["status"] == "healthy"
            else {"status": "unknown", "applied": None, "head": None}
        )

    overall = (
        "healthy"
        if database["status"] == "healthy" and migrations["status"] == "up_to_date"
        else "unhealthy"
    )
    return {
        "release_id": settings.release_id or None,
        "installed_release_id": installed_release_id(),
        "app_version": __version__,
        "database": database,
        "migrations": migrations,
        "overall": overall,
    }
