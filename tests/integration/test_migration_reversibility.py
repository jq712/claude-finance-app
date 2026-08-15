"""Adversarial coverage for `alembic downgrade -1` / `upgrade head` on the
roles-and-grants migration (0002_fc8bd0e714f9). Handoff §29's Milestone 1
exit criteria requires "migration test strategy exists"; the existing
`tests/integration/test_migrations.py` only checks the forward-applied
state. Nothing exercises downgrade, which is the migration most likely to
leave the database in a broken state — it drops five live database roles
and their default-privilege entries.

These tests are destructive to the dev database's role set (they actually
drop and recreate finance_owner/finance_app/finance_agent/
finance_observer/finance_backup) and shell out to the real `alembic` CLI
against the container configured in `deploy/compose.dev.yaml`. They must
run serially against a dev/CI database only — never anything with real
data — and always leave the database back at head, even on failure.
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from finance_app.db.session import dispose_engine

pytestmark = pytest.mark.integration

ALEMBIC_DSN = "postgresql+psycopg://finance_migrator:devpassword@localhost:5433/finance_dev"
ROLES = ["finance_owner", "finance_app", "finance_agent", "finance_observer", "finance_backup"]


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": ALEMBIC_DSN}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        timeout=60,
    )


@pytest.fixture
def restore_to_head():
    """Guarantee the database is back at head even if a test fails
    mid-assertion, since later tests/fixtures assume the roles exist."""
    yield
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, (
        f"failed to restore migrations to head after a reversibility test: "
        f"{result.stdout}\n{result.stderr}"
    )
    # Any connection this process pooled via the shared app engine before
    # the downgrade/upgrade round-trip is now stale relative to the
    # recreated finance_app role — see dispose_engine()'s docstring.
    dispose_engine()


def test_downgrade_then_upgrade_round_trips_cleanly(restore_to_head, role_engine) -> None:
    down = _run_alembic("downgrade", "-1")
    assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"

    # While downgraded, the least-privilege roles must not exist at all —
    # proves `downgrade` actually drops them rather than silently no-oping.
    migrator = role_engine("finance_migrator")
    with migrator.connect() as conn:
        remaining = {
            row[0]
            for row in conn.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                {"roles": ROLES},
            )
        }
    assert remaining == set(), f"downgrade left roles behind: {remaining}"

    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, f"upgrade failed:\n{up.stdout}\n{up.stderr}"

    with migrator.connect() as conn:
        recreated = {
            row[0]
            for row in conn.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                {"roles": ROLES},
            )
        }
    assert recreated == set(ROLES), (
        f"upgrade did not recreate all roles: missing {set(ROLES) - recreated}"
    )


def test_round_trip_does_not_duplicate_default_privilege_entries(
    restore_to_head, role_engine
) -> None:
    """`ALTER DEFAULT PRIVILEGES ... GRANT` is idempotent per (grantor,
    schema, role) triple in pg_default_acl — but only if downgrade's REVOKE
    actually clears the old entries first. If it doesn't, a repeated
    downgrade/upgrade cycle could still leave the ACL correct (Postgres
    dedupes the privilege bits within one row) but this test pins that
    down mechanically rather than assuming it."""
    migrator = role_engine("finance_migrator")

    def _default_acl_row_count() -> int:
        with migrator.connect() as conn:
            return conn.execute(
                text(
                    "SELECT count(*) FROM pg_default_acl d "
                    "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
                    "WHERE n.nspname = ANY(:schemas)"
                ),
                {"schemas": ["plaid", "user", "finance", "agent", "ops"]},
            ).scalar_one()

    before = _default_acl_row_count()

    for _ in range(2):
        d = _run_alembic("downgrade", "-1")
        assert d.returncode == 0, f"downgrade failed:\n{d.stdout}\n{d.stderr}"
        u = _run_alembic("upgrade", "head")
        assert u.returncode == 0, f"upgrade failed:\n{u.stdout}\n{u.stderr}"

    after = _default_acl_row_count()
    assert after == before, (
        f"pg_default_acl row count changed after two downgrade/upgrade cycles "
        f"({before} -> {after}) — default-privilege entries are accumulating "
        "rather than being cleanly replaced"
    )


def test_downgrade_revokes_access_for_a_connection_opened_before_it_ran(
    restore_to_head, role_engine
) -> None:
    """Operational hazard, not a code bug: PostgreSQL allows DROP ROLE on a
    role with an already-open, already-authenticated session — the
    existing connection keeps working until it needs to re-authenticate,
    but the role is gone for every *new* connection from that moment.
    A `finance_app` connection pool that is alive during a live downgrade
    will silently stop being able to open new connections mid-migration,
    with no error surfaced by the migration itself. This test proves the
    mechanism so the runbook can call it out explicitly."""
    app_engine = role_engine("finance_app")
    with app_engine.connect() as held_open:
        held_open.execute(text("SELECT 1"))

        down = _run_alembic("downgrade", "-1")
        assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"

        # The already-open connection survives the role's own deletion...
        assert held_open.execute(text("SELECT 1")).scalar_one() == 1, (
            "expected the pre-existing connection to keep working after its "
            "role is dropped out from under it — if this now fails, "
            "PostgreSQL's behavior here has changed and the runbook note "
            "this test exists to justify is wrong"
        )

    # ...but a brand new connection attempt as finance_app must fail,
    # because the role no longer exists.
    fresh_engine = role_engine("finance_app")
    with pytest.raises(OperationalError), fresh_engine.connect() as conn:
        conn.execute(text("SELECT 1"))
