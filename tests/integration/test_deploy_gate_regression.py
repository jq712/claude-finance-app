"""Adversarial regression tests for `finops deploy`'s promotion gate and
for migration 0005's data safety (QA round 2, Milestone 7 — handoff §19,
ADR-008).

Round 1 found that two concurrent deploys could both become `current`
(QA-3) and that a failed deploy rolled back to the wrong release (QA-2).
The round-2 fix added `ops.releases.replaces_release_id`, a partial unique
index (`migrations/versions/0005_a1c3e9f4d2b7_release_rollback_safety.py`)
and a `pg_try_advisory_xact_lock` around promotion. The database invariant
now holds. What the fix did not do is make the *system* consistent or the
*operator* correctly informed when that guard actually fires, and it left
the migration unable to apply to any database that already hit the bug.

All `xfail(strict=True)`; delete the marker once fixed, never the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from finance_app.ops import status
from finance_app.ops.db import observer_session_scope
from finance_app.ops.release import _DEPLOY_PROMOTION_LOCK_KEY
from tests.conftest import role_dsn

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _healthy_selfcheck_run_release(  # noqa: ANN001, ANN002, ANN003, ARG001
    release_path, *args, env=None, **kwargs
):
    """Stands in for `run_release`: every call succeeds, and a `finance
    selfcheck --json` run (ADR-016 D3 — `ops.status.probe_release`)
    reports the pinned `RELEASE_ID` as healthy — see the identical helper
    in tests/integration/test_finops_cli_regression.py."""
    if "selfcheck" in args:
        reported = (env or {}).get("RELEASE_ID")
        payload = json.dumps(
            {"release_id": reported, "installed_release_id": reported, "overall": "healthy"}
        )
        return subprocess.CompletedProcess(list(args), 0, payload + "\n", "")
    return subprocess.CompletedProcess(list(args), 0, "", "")


def _make_release_dir(root: Path, release_id: str) -> None:
    """A real (empty of a real binary — every test here mocks
    `run_release`, so nothing actually executes `.venv/bin/finance`)
    release directory, real enough for `release_is_installed`/
    `repoint_current`'s filesystem checks (ADR-019) to pass naturally
    rather than needing to be mocked away too."""
    bin_dir = root / "releases" / release_id / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "finance").touch()


runner = CliRunner()


@pytest.fixture
def two_healthy_releases(role_engine, tmp_path):
    """`aaaaaaa` (previous) -> `bbbbbbb` (current), the ordinary state a
    third deploy starts from. Also builds real release directories for
    both under a `tmp_path` release root (and points `current` at
    `bbbbbbb`, matching the bookkeeping below) — ADR-019's `deploy`/
    `rollback` touch the filesystem between health-check and bookkeeping,
    so these need to be real, not just DB rows."""
    engine = role_engine("finance_app")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        for release_id, release_status in (("a" * 7, "previous"), ("b" * 7, "current")):
            conn.execute(
                text(
                    "INSERT INTO ops.releases "
                    "(release_id, artifact_ref, status, health_check_status) "
                    "VALUES (:r, :i, :s, 'healthy')"
                ),
                {"r": release_id, "i": f"img:{release_id}", "s": release_status},
            )
    _make_release_dir(tmp_path, "a" * 7)
    _make_release_dir(tmp_path, "b" * 7)
    (tmp_path / "current").symlink_to(Path("releases") / ("b" * 7), target_is_directory=True)
    yield engine, tmp_path
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-21: when the promotion advisory lock is contended, mark_healthy records the "
        "release as `failed` but `finops deploy` still prints success and exits 0."
    ),
)
def test_deploy_reports_failure_when_the_promotion_lock_is_contended(
    two_healthy_releases, monkeypatch
) -> None:
    """`mark_healthy` treats a contended `pg_try_advisory_xact_lock` as
    "record this release as failed and return". It returns `None` either
    way, so `cli/finops.py:deploy` cannot tell the difference: it decides
    `deployed_ok` purely from the health-check dict, which was healthy.

    The result is a three-way split-brain, reproduced verbatim against the
    dev database with a second connection holding the lock::

        $ finops deploy ccccccc
        deployed ccccccc (ghcr.io/jq712/claude-finance-app:ccccccc)   # exit 0
        ops.releases: [('aaaaaaa','previous'), ('bbbbbbb','current'),
                       ('ccccccc','failed','concurrent deploy detected ...')]

    `docker compose up -d app` already ran with `RELEASE_ID=ccccccc`, so
    the container on the host *is* `ccccccc`; the database says `bbbbbbb`
    is current; and the operator was told the deploy succeeded. A later
    `finops rollback` then resolves its target from bookkeeping that does
    not describe reality.

    A lost promotion must be loud: non-zero exit and an error message, or
    a retry — never a green "deployed".
    """

    import finance_app.cli.finops as finops_module

    engine, release_root = two_healthy_releases
    _make_release_dir(release_root, "c" * 7)
    monkeypatch.setattr(finops_module, "run_release", _healthy_selfcheck_run_release)
    monkeypatch.setattr(finops_module, "latest_successful_backup", lambda: object())

    # A concurrent `finops deploy` on the same host holds the promotion
    # lock. Released explicitly and the engine disposed in `finally`: a
    # *session*-level advisory lock survives SQLAlchemy returning the
    # connection to the pool (pool return issues ROLLBACK, which does not
    # drop session-scoped advisory locks), so leaking one here would make
    # every later `mark_healthy` in this pytest process silently record
    # its release as `failed` — which is itself a neat demonstration of
    # the defect under test.
    engine_holding_lock = create_engine(role_dsn("finance_app"))
    holder = engine_holding_lock.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _DEPLOY_PROMOTION_LOCK_KEY})
        holder.commit()

        result = runner.invoke(
            finops_module.app, ["deploy", "c" * 7, "--release-root", str(release_root)]
        )
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _DEPLOY_PROMOTION_LOCK_KEY})
        holder.commit()
        holder.close()
        engine_holding_lock.dispose()

    with engine.begin() as conn:
        statuses = dict(
            conn.execute(text("SELECT release_id, status FROM ops.releases")).all()  # type: ignore[arg-type]
        )

    promoted = statuses.get("c" * 7) == "current"
    assert promoted or result.exit_code != 0, (
        "deploy reported success while the release was recorded as "
        f"{statuses.get('c' * 7)!r} and {statuses} shows current is still "
        f"{'b' * 7}; exit code was {result.exit_code}, output was {result.output!r}"
    )


def test_deploy_health_check_does_not_depend_on_the_process_working_directory(
    tmp_path, monkeypatch
) -> None:
    """QA-22, fixed by ADR-016 D3 rather than worked around: the old
    `deploy_health_check` called `status.migration_status`, which builds
    `Config("alembic.ini")` from a path relative to the process CWD —
    inside the `deploy` Compose service that CWD is
    `${FINANCE_APP_DIR:-/opt/finance-app}`, which `docs/runbooks/deploy.md`
    §1.5 populates with `deploy/` only, not `alembic.ini`/`migrations/`, so
    the migration check always came back `"unknown"` and failed the gate
    on a perfectly healthy release.

    D3's fix isn't a CWD-independent lookup from the deploy container —
    that container is pinned to the *previous* release and structurally
    cannot know the new one's migration head either way (see
    `deploy_health_check`'s docstring). Instead `deploy_health_check` no
    longer calls `migration_status` itself at all: it delegates entirely
    to `probe_release`, a one-shot run of the release image's own `finance
    selfcheck`, which answers this from inside the only process that
    actually knows. This test proves the CWD independence directly: it
    `chdir`s somewhere with no `alembic.ini` and confirms that has zero
    effect on the result, because nothing in this call path ever reads
    that file.
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "alembic.ini").exists()

    payload = json.dumps(
        {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "healthy"}
    )

    def fake_run_release(  # noqa: ANN001, ANN002, ANN003, ARG001
        release_path, *args, env=None, **kwargs
    ):
        return subprocess.CompletedProcess(list(args), 0, payload + "\n", "")

    with observer_session_scope() as session:
        health = status.deploy_health_check(
            session,
            release_id="abc1234",
            run_release_fn=fake_run_release,
        )

    assert health["overall"] == "healthy", (
        f"the deploy health gate depends on the process working directory: {health}"
    )


def test_rollback_refuses_to_promote_a_target_that_fails_its_own_selfcheck(
    two_healthy_releases, monkeypatch
) -> None:
    """`_do_rollback` used to `docker compose up -d app` and then
    unconditionally promote the rollback target — under ADR-016 D2 that
    starts a one-shot container that prints `finance --help` and exits,
    verifying nothing, so a rollback target that is itself broken (e.g. a
    schema drift the forward migration introduced) would have been
    silently promoted to `current` anyway. It must instead run
    `probe_release` against the target and refuse to touch bookkeeping if
    that fails."""
    import finance_app.cli.finops as finops_module

    engine, release_root = two_healthy_releases

    def unhealthy_run_release(release_path, *args, env=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
        if "selfcheck" in args:
            reported = (env or {}).get("RELEASE_ID")
            payload = json.dumps(
                {"release_id": reported, "installed_release_id": reported, "overall": "unhealthy"}
            )
            return subprocess.CompletedProcess(list(args), 1, payload + "\n", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(finops_module, "run_release", unhealthy_run_release)

    result = runner.invoke(finops_module.app, ["rollback", "--release-root", str(release_root)])

    with engine.begin() as conn:
        statuses = dict(
            conn.execute(text("SELECT release_id, status FROM ops.releases")).all()  # type: ignore[arg-type]
        )

    assert result.exit_code != 0, "rollback reported success against an unhealthy target"
    assert statuses["b" * 7] == "current", (
        f"rollback promoted an unhealthy target instead of leaving current alone: {statuses}"
    )
    assert statuses["a" * 7] == "previous"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-23: migration 0005's unique index cannot be created on a database that "
        "already contains the duplicate rows the bug it fixes produced."
    ),
)
def test_migration_0005_applies_to_a_database_that_already_hit_the_bug() -> None:
    """0005 creates `ux_releases_current_previous` with no data-cleanup
    step. Any production database that ran the pre-fix `mark_healthy` and
    lost the QA-3 race already has two `status='current'` rows — which is
    the entire reason the index is being added — so `CREATE UNIQUE INDEX`
    fails::

        sqlalchemy.exc.IntegrityError: (psycopg.errors.UniqueViolation)
        could not create unique index "ux_releases_current_previous"
        DETAIL:  Key (status)=(current) is duplicated.

    `finops deploy` runs migrations as its preflight step, so the upgrade
    that is supposed to *fix* the race is the one that cannot be applied,
    and `finops` deliberately exposes no SQL surface for the operator to
    repair `ops.releases` by hand.

    CI's `migration-preflight` job cannot catch this: it applies the
    previous release's migrations to an *empty* database, so there are
    never any rows to conflict. The migration needs to demote extra
    `current`/`previous` rows to `history` before creating the index.
    """
    admin_url = role_dsn("finance_migrator")
    scratch = "qa_release_index_backfill"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    scratch_url = admin_url.rsplit("/", 1)[0] + f"/{scratch}"

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "ALEMBIC_DATABASE_URL": scratch_url},
            cwd=str(_REPO_ROOT),
            timeout=120,
        )

    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{scratch}"'))

        before = alembic("upgrade", "b2518380bc18")
        assert before.returncode == 0, f"setup upgrade failed:\n{before.stdout}\n{before.stderr}"

        scratch_engine = create_engine(scratch_url)
        try:
            with scratch_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO ops.releases (release_id, image_ref, status) VALUES "
                        "('aaaaaaa', 'img:a', 'current'), ('bbbbbbb', 'img:b', 'current')"
                    )
                )
        finally:
            scratch_engine.dispose()

        result = alembic("upgrade", "head")
        assert result.returncode == 0, (
            "migration 0005 cannot be applied to a database that already lost the "
            f"QA-3 race:\n{result.stdout}\n{result.stderr}"
        )
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
