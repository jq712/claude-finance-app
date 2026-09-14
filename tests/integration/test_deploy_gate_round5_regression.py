"""Adversarial regression tests for the QA-41/QA-42 rollback fixes (QA
round 5, Milestone 7 — handoff §19, ADR-008, ADR-016 D2/D3).

Round 4 asked "is the release that was health-verified the release that
gets promoted?" and the fix answered the *identity* half: `_do_rollback`
now resolves one `ops.releases` row, probes it, and passes that row's
primary key to `release_ops.rollback`. Round 5 attacks what the fix did
not close:

* QA-47 — `rollback` never takes the deploy-promotion advisory lock that
  `mark_healthy` takes (QA-3), and never re-checks that `current` is still
  what it was when the target was resolved. A deploy that completes during
  the probe window is therefore demoted to `rolled_back` by a rollback
  that never probed it and never knew it existed, while both commands
  report success.
* QA-48 — QA-42 made `get_previous` return the *current* release whenever
  the newest attempt is `pending` or `failed`, so `_do_rollback`'s target
  is frequently the release that is already running. `release_ops.rollback`
  correctly treats that as a no-op — and `finops rollback` then prints a
  green "rolled back to <sha>" and exits 0 having changed nothing, in the
  single most common incident sequence there is (a deploy just failed).

Defect tests are `xfail(strict=True)`, matching the convention in
`test_deploy_gate_round4_regression.py`: delete the marker once fixed,
never the test.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from finance_app.ops import release as release_ops
from finance_app.ops import status as status_module

pytestmark = pytest.mark.integration

runner = CliRunner()


def _rows(engine) -> dict[str, str]:  # noqa: ANN001
    """release_id -> status. Every release id below is distinct, so
    collapsing into a dict is safe here (unlike in general — see
    `test_ops_release.py::test_rollback_promotes_the_exact_row_probed...`,
    where `release_id` deliberately recurs)."""
    with engine.begin() as conn:
        return dict(conn.execute(text("SELECT release_id, status FROM ops.releases")).all())  # type: ignore[arg-type]


def _healthy_probe(*, release_id: str, **_kwargs):  # noqa: ANN202
    return {"status": "healthy", "reported_release_id": release_id, "detail": {}}


def _make_release_dir(root: Path, release_id: str) -> None:
    bin_dir = root / "releases" / release_id / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "finance").touch()


@pytest.fixture
def deploy_in_flight(role_engine, tmp_path):  # noqa: ANN001, ANN201
    """`aaaaaaa` (previous) -> `bbbbbbb` (current), plus `ccccccc` left
    `pending` by a `finops deploy` that is *still running* — one minute
    old, well inside `_STALE_PENDING_DEPLOY_MINUTES`, so it is a live
    deploy rather than the orphaned row QA-42's reaper is about.

    `deployed_at` is pinned explicitly so `get_previous`'s
    `ORDER BY deployed_at DESC, id DESC` resolves deterministically
    regardless of how the rows were inserted. Also builds real release
    directories for all three under a `tmp_path` release root, with
    `current` pointed at `bbbbbbb` — `_do_rollback` repoints `current` for
    real between its probe and its bookkeeping write under ADR-019, even
    on the paths these tests exercise where that write is ultimately
    refused (QA-47/QA-48)."""
    engine = role_engine("finance_app")
    now = datetime.datetime.now(datetime.UTC)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.execute(
            text(
                "INSERT INTO ops.releases "
                "(release_id, artifact_ref, status, health_check_status, replaces_release_id, "
                " deployed_at) VALUES "
                "('aaaaaaa', 'img:aaaaaaa', 'previous', 'healthy', NULL, :t0), "
                "('bbbbbbb', 'img:bbbbbbb', 'current', 'healthy', 'aaaaaaa', :t1), "
                "('ccccccc', 'img:ccccccc', 'pending', NULL, 'bbbbbbb', :t2)"
            ),
            {
                "t0": now - datetime.timedelta(minutes=30),
                "t1": now - datetime.timedelta(minutes=20),
                "t2": now - datetime.timedelta(minutes=1),
            },
        )
    for release_id in ("aaaaaaa", "bbbbbbb", "ccccccc"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)
    yield engine, tmp_path
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))


# ---------------------------------------------------------------------------
# QA-47 — the promotion race the QA-41 fix moved rather than closed.
# ---------------------------------------------------------------------------


def test_rollback_does_not_demote_a_release_promoted_during_its_probe_window(
    deploy_in_flight, monkeypatch
) -> None:
    """Verify-then-act, one layer up from QA-41.

    QA-41 made `_do_rollback` pass a specific row id so the promotion
    cannot drift off the probed row. Nothing, though, re-validates the
    *other* side of the swap: `rollback` reads `get_current()` fresh at
    promotion time and demotes whatever it finds, even if that is a
    release which became `current` after this rollback resolved its
    target.

    The window is not theoretical — it is exactly the window ADR-016 D3's
    probe opens: a `docker compose run` of the release image, seconds to
    minutes. And QA-42 made the sequence *more* reachable, because a
    `pending` row (a live deploy) now steers `get_previous` to the
    currently-running release, so the natural time to issue this rollback
    is precisely while a deploy is in flight.

    Sequence reproduced here:

    1. `finops rollback` resolves its target — `bbbbbbb`, because the
       newest attempt (`ccccccc`) is `pending` (QA-42).
    2. During the probe, the in-flight deploy of `ccccccc` finishes and
       calls `mark_healthy`: `aaaaaaa` -> history, `bbbbbbb` -> previous,
       `ccccccc` -> current. That deploy exits 0 and prints
       "deployed ccccccc".
    3. The rollback promotes `bbbbbbb` and demotes whatever is current —
       `ccccccc`, a release it never probed, which had just passed its own
       health check, and which nothing has ever found unhealthy. It exits
       0 and prints "rolled back to bbbbbbb".

    `mark_healthy` guards its own half of this with
    `pg_try_advisory_xact_lock` (QA-3) and records "concurrent deploy
    detected" rather than racing. `rollback` — the other writer of
    `status = 'current'` — takes no lock at all, so the guard has a hole
    exactly the shape of this test.
    """
    engine, release_root = deploy_in_flight

    def probe_and_let_the_deploy_land(*, release_id: str, **_kwargs):  # noqa: ANN202
        with Session(engine) as session:
            landing_id = session.execute(
                text("SELECT id FROM ops.releases WHERE release_id = 'ccccccc'")
            ).scalar_one()
            row = session.get(release_ops.Release, landing_id)
            assert row is not None
            release_ops.mark_healthy(session, row)
            session.commit()
        return _healthy_probe(release_id=release_id)

    monkeypatch.setattr(status_module, "probe_release", probe_and_let_the_deploy_land)
    import finance_app.cli.finops as finops_module

    result = runner.invoke(finops_module.app, ["rollback", "--release-root", str(release_root)])

    statuses = _rows(engine)
    assert statuses["ccccccc"] != "rolled_back", (
        "a release that became `current` after this rollback resolved and probed its "
        f"target was silently demoted: {statuses}; rollback exited {result.exit_code} "
        f"saying {result.output.strip()!r}"
    )


def test_rollback_respects_the_deploy_promotion_advisory_lock(deploy_in_flight) -> None:
    """The QA-3 invariant stated directly: whoever decides which row is
    `status = 'current'` must hold `_DEPLOY_PROMOTION_LOCK_KEY` while
    doing so. `mark_healthy` does and degrades gracefully ("concurrent
    deploy detected while promoting to current") when it cannot get it.
    `rollback` is the only other writer of that column and ignores the
    lock entirely, so the mutual exclusion is one-sided and therefore not
    mutual exclusion.

    Deterministic, no timing: a separate session takes the lock in an open
    transaction, then `rollback` is called. It must not promote. Calls
    `release_ops.rollback` directly, bypassing the CLI and every
    filesystem operation `_do_rollback` adds under ADR-019 — this test is
    about the database-level lock, not the symlink."""
    engine, _release_root = deploy_in_flight
    with Session(engine) as holder, Session(engine) as actor:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": release_ops._DEPLOY_PROMOTION_LOCK_KEY},
        )
        # A correct fix uses the non-blocking `pg_try_advisory_xact_lock`,
        # like `mark_healthy`. This bounds the damage if someone reaches
        # for the blocking form instead: a failed test, not a hung CI job.
        actor.execute(text("SET LOCAL statement_timeout = '15s'"))
        target_id = actor.execute(
            text("SELECT id FROM ops.releases WHERE release_id = 'aaaaaaa'")
        ).scalar_one()

        promoted_anyway = True
        try:
            release_ops.rollback(actor, release_row_id=target_id)
            actor.flush()
        except Exception:  # noqa: BLE001 - any refusal is acceptable; silent success is not
            promoted_anyway = False
            actor.rollback()
        else:
            promoted = actor.execute(
                text("SELECT status FROM ops.releases WHERE release_id = 'aaaaaaa'")
            ).scalar_one()
            promoted_anyway = promoted == "current"
            actor.rollback()
        holder.rollback()

    assert not promoted_anyway, (
        "rollback promoted a release to `current` while another session held the "
        "deploy-promotion advisory lock — the QA-3 guard `mark_healthy` respects"
    )


# ---------------------------------------------------------------------------
# QA-48 — a rollback that changes nothing still reports success.
# ---------------------------------------------------------------------------


@pytest.fixture
def failed_latest_deploy(role_engine, tmp_path):  # noqa: ANN001, ANN201
    """The designed QA-2 state: a deploy just failed, so `current` is
    still the release that is actually running. Real release directories
    for all three, `current` already pointing at `bbbbbbb` — matching
    what a rollback resolving to the no-op case should find on disk too."""
    engine = role_engine("finance_app")
    now = datetime.datetime.now(datetime.UTC)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.execute(
            text(
                "INSERT INTO ops.releases "
                "(release_id, artifact_ref, status, health_check_status, replaces_release_id, "
                " deployed_at) VALUES "
                "('aaaaaaa', 'img:aaaaaaa', 'previous', 'healthy', NULL, :t0), "
                "('bbbbbbb', 'img:bbbbbbb', 'current', 'healthy', 'aaaaaaa', :t1), "
                "('ccccccc', 'img:ccccccc', 'failed', 'unhealthy', 'bbbbbbb', :t2)"
            ),
            {
                "t0": now - datetime.timedelta(minutes=30),
                "t1": now - datetime.timedelta(minutes=20),
                "t2": now - datetime.timedelta(minutes=1),
            },
        )
    for release_id in ("aaaaaaa", "bbbbbbb", "ccccccc"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)
    yield engine, tmp_path
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))


def test_rollback_does_not_claim_success_when_it_changed_nothing(
    failed_latest_deploy, monkeypatch
) -> None:
    """An operator whose deploy just failed runs `finops rollback`. The
    resolved target is `bbbbbbb`, which is already `current`, so
    `release_ops.rollback` returns it untouched (correctly — nothing to
    demote). ADR-016 D2 removed `_do_rollback`'s `docker compose up`, so
    no container action happens either: the command performs one selfcheck
    probe and nothing else.

    It then prints `rolled back to bbbbbbb` in green and exits 0. In an
    incident that reads as "production moved", and the `ops.releases`
    audit trail records no rollback at all, because there wasn't one.

    A narrow, auditable production interface (this module's own docstring)
    must not report a state change it did not make. Either say so
    explicitly ("bbbbbbb is already current; nothing to roll back") or
    exit non-zero — do not let the two outcomes be indistinguishable.
    """
    engine, release_root = failed_latest_deploy
    before = _rows(engine)
    monkeypatch.setattr(status_module, "probe_release", _healthy_probe)
    import finance_app.cli.finops as finops_module

    result = runner.invoke(finops_module.app, ["rollback", "--release-root", str(release_root)])

    after = _rows(engine)
    assert before == after, "precondition: this branch is expected to change no bookkeeping"
    unqualified_success = result.exit_code == 0 and not any(
        phrase in result.output.lower()
        for phrase in ("already", "no-op", "no rollback", "unchanged", "nothing to")
    )
    assert not unqualified_success, (
        "`finops rollback` reported an unqualified success for an operation that changed "
        f"no bookkeeping and started no container: {result.output.strip()!r} (exit "
        f"{result.exit_code}); ops.releases before == after == {after}"
    )
