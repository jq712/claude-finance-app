"""Round-trip test for migration 0006 (`ops.releases.image_ref` ->
`artifact_ref`, ADR-019). Pins the data-loss assessment in that
migration's own docstring: `ALTER TABLE ... RENAME COLUMN` is
catalog-only, so a value inserted before a downgrade must survive under
the old name and read back correctly after a re-upgrade."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import role_dsn

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _alembic(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": role_dsn("finance_migrator")}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        timeout=60,
    )


@pytest.fixture
def app_engine(role_engine):
    return role_engine("finance_app")


@pytest.fixture
def migrator_engine(role_engine):
    return role_engine("finance_migrator")


def test_artifact_ref_round_trips_through_downgrade_and_upgrade(
    app_engine, migrator_engine
) -> None:
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases WHERE release_id = 'qa-0006-roundtrip'"))
        conn.execute(
            text(
                "INSERT INTO ops.releases (release_id, artifact_ref, status) "
                "VALUES ('qa-0006-roundtrip', '/opt/finance/releases/qa-0006-roundtrip', "
                "'history')"
            )
        )

    try:
        with migrator_engine.begin() as conn:
            conn.execute(text("SELECT 1"))  # sanity: role can connect before downgrading

        # Targeted at 0006's own down_revision, not a relative `-1`: a
        # relative count silently downgrades whatever the *current* head
        # happens to be, which stopped being 0006 once migration 0007
        # landed on top of it — `-1` from a later head undoes the wrong
        # migration instead of this one.
        down = _alembic("downgrade", "a1c3e9f4d2b7")
        assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"

        with app_engine.begin() as conn:
            value = conn.execute(
                text("SELECT image_ref FROM ops.releases WHERE release_id = 'qa-0006-roundtrip'")
            ).scalar_one()
        assert value == "/opt/finance/releases/qa-0006-roundtrip", (
            f"the value did not survive under the old column name after downgrade — got {value!r}"
        )

        up = _alembic("upgrade", "head")
        assert up.returncode == 0, f"upgrade failed:\n{up.stdout}\n{up.stderr}"

        with app_engine.begin() as conn:
            value = conn.execute(
                text("SELECT artifact_ref FROM ops.releases WHERE release_id = 'qa-0006-roundtrip'")
            ).scalar_one()
        assert value == "/opt/finance/releases/qa-0006-roundtrip", (
            f"the value did not survive the round trip back to the new column name — got {value!r}"
        )
    finally:
        with app_engine.begin() as conn:
            conn.execute(text("DELETE FROM ops.releases WHERE release_id = 'qa-0006-roundtrip'"))
