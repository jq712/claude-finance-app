"""Deterministic post-deploy smoke check (ADR-016 D3).

Runs *inside* the image under deployment, never from the `deploy`
control-plane container — that container is pinned to `${RELEASE_ID:-latest}`
from before the new release id was known, so it always runs the *previous*
release's image and can never report what the *new* one's migration head is
or even confirm which image actually started. Only the release image itself
can answer that, which is why `finops deploy` shells out to
`finance selfcheck --json` (see `ops.status.probe_release`) rather than
probing anything from the deploy container's own process.

Reads as `finance_app` (`db.session.session_scope`) — the same role, same
credential (`FINANCE_APP_DB_PASSWORD`), the one-shot `app` job already
holds. Never calls a model provider and never needs a provider API key, so
selfcheck stays usable even when `AGENT_PROVIDER`'s credential is absent or
wrong — that failure belongs to `finance chat`, not to the deploy gate.
"""

from __future__ import annotations

from typing import Any

from finance_app import __version__
from finance_app.config.settings import get_settings
from finance_app.db.session import session_scope
from finance_app.ops.status import db_status, migration_status


def selfcheck() -> dict[str, Any]:
    """{"release_id", "app_version", "database": {...}, "migrations": {...},
    "overall": "healthy"|"unhealthy"}. `overall` is healthy only when the
    database is reachable *and* this image's migration head matches what's
    applied — the same bar `deploy_health_check` used to hold the deploy
    container to, now answered by the party that actually knows it."""
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
        "app_version": __version__,
        "database": database,
        "migrations": migrations,
        "overall": overall,
    }
