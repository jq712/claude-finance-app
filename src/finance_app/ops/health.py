"""Sanitized operational health reporting (handoff §10, §23).

`check_health()` is what `finance-health.timer` (deploy/systemd/) runs via
`python -m finance_app.ops.health`: it computes the same aggregate health
view as `finops health`, but additionally writes an `ops.errors` row when
unhealthy, so a failure becomes part of the auditable operational history
rather than just a transient timer exit code. That write needs the
`finance_app` role (via `db/session.py`), so this module is deliberately
*not* what the `finops health` CLI command calls — `finops` stays strictly
read-only via `finance_observer` (see `ops/db.py`); it calls
`ops.status.aggregate_health` directly instead. This module is the
systemd-timer writer path, not the interactive diagnostic path.
"""

from __future__ import annotations

import logging
import sys

from finance_app.config.settings import get_settings
from finance_app.db.models.ops import OperationalError
from finance_app.db.session import session_scope
from finance_app.ops import status
from finance_app.ops.logging import log_event

logger = logging.getLogger(__name__)


def check_health() -> dict[str, object]:
    """Run the aggregate health check and, if unhealthy, record an
    `ops.errors` row describing which component(s) failed. Returns the
    same dict `finops health` prints, so callers get one source of truth
    for both the human/machine CLI output and the systemd timer's exit
    status."""
    settings = get_settings()
    with session_scope() as session:
        result = status.aggregate_health(session, settings)
        if result["overall"] != "healthy":
            session.add(
                OperationalError(
                    category="health_check_failed",
                    message="finops health reported an unhealthy component",
                    context={
                        "database": result["database"],
                        "migrations": result["migrations"],
                        "sync": result["sync"],
                        "backup": result["backup"],
                    },
                )
            )
    log_event(
        logger,
        logging.INFO if result["overall"] == "healthy" else logging.WARNING,
        "health check completed",
        context={"overall": result["overall"]},
    )
    return result


if __name__ == "__main__":
    outcome = check_health()
    print(outcome)  # noqa: T201 - captured by journald as this unit's stdout
    sys.exit(0 if outcome["overall"] == "healthy" else 1)
