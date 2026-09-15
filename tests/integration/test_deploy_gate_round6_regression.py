"""Adversarial regression tests for ADR-019's bare-metal deploy/rollback
state machine (QA round 6, Milestone 7 — handoff §19, ADR-008, ADR-019).

Rounds 3–5 attacked a deploy path whose only production-visible mutation
was `docker compose up`, committed *before* the bookkeeping write but
undone implicitly by the next `up`. ADR-019 replaces that with a
`current` symlink plus a `systemctl restart`, and `cli/finops.py`
deliberately keeps the same ordering: mutate the filesystem, verify it,
*then* commit `ops.releases`. `_do_rollback`'s docstring makes a specific
safety claim about that ordering:

    A crash between the two leaves bookkeeping stale but never wrong in a
    way that blocks a retry — re-running rollback resolves the same target
    again, finds the symlink already correct (a no-op), and finishes the
    bookkeeping write it didn't reach last time.

Round 6 attacked that claim, and the analogous claim on `deploy`'s
promotion block. The common root cause of every defect below: the
QA-3/QA-47 advisory lock and `expected_current_row_id` check protect the
*database* half of a promotion, and were evaluated only after the
*filesystem* half had already happened, with no failure path reverting
it. All four are now fixed — `cli/finops.py` captures the pre-promotion
symlink target and reverts to it (or removes `current` entirely, for a
first-ever deploy with nothing to revert to) whenever a post-restart
probe fails or a database promotion is declined/contended, and `deploy`
now checks the promoted row's actual resulting status rather than only
the health-check result. Every test in this file asserts the fixed
behavior directly (no `xfail` markers remain; per this project's
convention, a marker is deleted once its defect is fixed, never the test):

* QA-49 (fixed) — `_do_rollback` used to repoint `current` and restart
  systemd, then discover its post-swap probe unhealthy, and raise without
  reverting — `finops rollback` reported "rollback target is not
  healthy" and exited 1 while production kept running that unhealthy
  target, with `finops restart` refusing (topology disagreement) and
  every retry re-raising the same error. Fixed by reverting `current` to
  its pre-attempt target before raising.
* QA-50 (fixed) — same ordering, reached through the QA-47 guard itself:
  a contended `RollbackContendedError` used to be raised *after*
  `repoint_current` had already switched production. Fixed the same way.
* QA-51 (fixed) — `finops deploy` used to exit 0 and print "deployed
  <sha>" when `mark_healthy` declined to promote (concurrent-deploy lock
  contention, QA-3's own documented degradation), because success was
  read from the health-check result rather than the row's actual
  resulting status. Fixed by checking `release.status == "current"`
  after calling `mark_healthy`.
* QA-52 (fixed) — a post-restart health-check failure on the *first*
  deploy after a first-ever release (`replaces_release_id IS NULL`) used
  to leave `current` pointing at the release that had just failed its
  health check, with no rollback attempted (`get_previous` correctly
  returns `None` when there is no prior release) and no mention that
  `current` had moved. Fixed by the same revert-to-previous-target logic
  as QA-49/50, which for a first-ever deploy removes the `current`
  symlink entirely rather than leaving it on the broken release.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from finance_app.ops import release as release_ops
from finance_app.ops.host import read_current_target
from tests.conftest import role_dsn

pytestmark = pytest.mark.integration

runner = CliRunner()


def _make_release_dir(root: Path, release_id: str) -> None:
    """A release directory real enough for `release_is_installed`/
    `repoint_current`'s filesystem checks, matching the helper in
    `test_deploy_gate_regression.py` — every test here stubs
    `run_release`, so nothing executes `.venv/bin/finance`."""
    bin_dir = root / "releases" / release_id / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "finance").touch()


def _selfcheck_payload(release_id: str | None, *, overall: str) -> str:
    return json.dumps(
        {
            "release_id": release_id,
            "installed_release_id": release_id,
            "overall": overall,
            "database": {"status": "healthy", "detail": None},
            "migrations": {"status": "up_to_date"},
        }
    )


def _healthy_run_release(release_path, *args, env=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
    """`run_release` stand-in where every invocation succeeds and every
    `finance selfcheck --json` reports the pinned `RELEASE_ID` healthy."""
    if "selfcheck" in args:
        payload = _selfcheck_payload((env or {}).get("RELEASE_ID"), overall="healthy")
        return subprocess.CompletedProcess(list(args), 0, payload + "\n", "")
    return subprocess.CompletedProcess(list(args), 0, "", "")


def _healthy_by_path_broken_via_current(release_path, *args, env=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
    """The failure mode `probe_release(via_current=True)` exists to catch:
    a release tree that selfchecks healthy when invoked at its own path,
    but not when invoked through `current` after the systemd restart — a
    unit that came up wrong, an environment only the unit supplies, or
    anything else that only manifests on the promoted path. Keyed on the
    invoked path's last component being `current`, exactly the distinction
    `via_current` makes."""
    if "selfcheck" not in args:
        return subprocess.CompletedProcess(list(args), 0, "", "")
    healthy = Path(str(release_path)).name != "current"
    payload = _selfcheck_payload(
        (env or {}).get("RELEASE_ID"), overall="healthy" if healthy else "unhealthy"
    )
    return subprocess.CompletedProcess(list(args), 0 if healthy else 1, payload + "\n", "")


@pytest.fixture
def finops_env(monkeypatch, role_engine):  # noqa: ANN001, ANN201
    """Points `finops`'s own two connections (`session_scope`'s
    `finance_app`, `observer_session_scope`'s `finance_observer`) at the
    same database `role_engine` uses, via `tests.conftest.role_dsn` — so
    these tests exercise the real `_do_rollback`/`deploy` bookkeeping
    writes rather than silently short-circuiting on an unreachable
    default DSN, and stay port-agnostic (conftest owns that).

    Disposes both cached engines on the way in *and* out: `db/session.py`
    and `ops/db.py` cache module-global engines keyed by DSN fingerprint,
    and leaving one built from this fixture's environment would leak into
    unrelated tests.

    Cleans `ops.releases` in teardown — `finance_dev` is shared."""
    monkeypatch.setenv("DATABASE_URL", role_dsn("finance_app"))
    monkeypatch.setenv("OBSERVER_DATABASE_URL", role_dsn("finance_observer"))
    from finance_app.db import session as session_module
    from finance_app.ops import db as observer_db_module

    session_module.dispose_engine()
    observer_db_module.dispose_observer_engine()
    engine = role_engine("finance_app")
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ops.releases"))
            conn.execute(text("DELETE FROM ops.backup_runs"))
        session_module.dispose_engine()
        observer_db_module.dispose_observer_engine()


def _seed_releases(engine, rows: list[tuple[str, str, str | None]]) -> None:  # noqa: ANN001
    """`(release_id, status, replaces_release_id)` in chronological order.
    `deployed_at` is pinned explicitly so `get_previous`'s
    `ORDER BY deployed_at DESC, id DESC` resolves deterministically."""
    now = datetime.datetime.now(datetime.UTC)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        for offset, (release_id, release_status, replaces) in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO ops.releases (release_id, artifact_ref, status, "
                    "health_check_status, replaces_release_id, deployed_at) "
                    "VALUES (:rid, :ref, :st, :hc, :rp, :t)"
                ),
                {
                    "rid": release_id,
                    "ref": f"/opt/finance/releases/{release_id}",
                    "st": release_status,
                    "hc": "healthy" if release_status in ("current", "previous") else None,
                    "rp": replaces,
                    "t": now - datetime.timedelta(minutes=60 - 10 * offset),
                },
            )


def _statuses(engine) -> dict[str, str]:  # noqa: ANN001
    """release_id -> status. Every release id in this module is distinct."""
    with engine.begin() as conn:
        return dict(conn.execute(text("SELECT release_id, status FROM ops.releases")).all())  # type: ignore[arg-type]


def _seed_fresh_backup(engine) -> None:  # noqa: ANN001
    """A successful, recent `ops.backup_runs` row — satisfies `deploy`'s
    backup gate (`status.backup_status`, via `observer_session_scope`)
    without needing to monkeypatch anything in `cli/finops.py`'s own
    namespace, which no longer imports a `latest_successful_backup` name
    to patch (the gate now goes through `status.backup_status` directly —
    security-review finding #7's fix)."""
    now = datetime.datetime.now(datetime.UTC)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.backup_runs"))
        conn.execute(
            text(
                "INSERT INTO ops.backup_runs (run_type, status, started_at, finished_at) "
                "VALUES ('backup', 'success', :t, :t)"
            ),
            {"t": now},
        )


# ---------------------------------------------------------------------------
# QA-49 — a rollback that declares its target unhealthy has already
# switched production onto it, and neither recovery command can undo that.
# ---------------------------------------------------------------------------


def test_rollback_refusing_an_unhealthy_target_does_not_leave_production_on_it(
    finops_env, tmp_path, monkeypatch
) -> None:
    """`_do_rollback`'s ordering is: probe the target by path, repoint
    `current` at it, restart systemd, re-probe *through* `current`, and
    only then write bookkeeping. The re-probe exists precisely because the
    first two probes cannot catch "what actually came up on the promoted
    path is broken" — so its failing is a designed-for, expected outcome,
    not an exotic one.

    When it does fail, `_do_rollback` raises
    `RollbackTargetUnhealthyError` and `finops rollback` prints "rollback
    target is not healthy" and exits 1. An operator reading that has been
    told the rollback did not happen. It did: `current` now resolves to
    the unhealthy target and the configured systemd units were restarted
    against it, while `ops.releases` still records the old release as
    `current`.

    The state that leaves is not merely stale, it is unrecoverable
    through `finops`:

    * `finops restart` refuses — `release_topology` disagrees (correctly).
    * `finops rollback` again resolves the same target, probes it by path
      (healthy — that was never the failing probe), finds the symlink
      already pointing at it, restarts, re-probes through `current`, and
      raises the identical error. Forever.

    which is exactly what `_do_rollback`'s docstring promises cannot
    happen ("never wrong in a way that blocks a retry").

    Either ordering fixes this — commit bookkeeping alongside the swap, or
    restore the previous symlink target before raising — but a refusal
    must not leave the filesystem holding a change the command says it
    declined to make.
    """
    engine = finops_env
    _seed_releases(engine, [("aaaaaaa", "previous", None), ("bbbbbbb", "current", "aaaaaaa")])
    for release_id in ("aaaaaaa", "bbbbbbb"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)

    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_release", _healthy_by_path_broken_via_current)

    result = runner.invoke(finops_module.app, ["rollback", "--release-root", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "not healthy" in result.output
    assert read_current_target(tmp_path) == "bbbbbbb", (
        "`finops rollback` reported its target unhealthy and exited 1, but `current` now "
        f"resolves to {read_current_target(tmp_path)!r} instead of the release that was "
        f"running before the command ran; bookkeeping still says {_statuses(engine)} — "
        "production is running a release this very command declared unhealthy, and both "
        "`finops restart` and a `finops rollback` retry now refuse permanently."
    )


def test_rollback_retry_after_an_unhealthy_post_swap_probe_is_not_a_dead_end(
    finops_env, tmp_path, monkeypatch
) -> None:
    """The second half of QA-49, asserted separately so it keeps its value
    once the symlink half is fixed: whatever state a failed rollback
    leaves behind, `finops` must offer a way out of it.

    Runs `finops rollback` twice against the QA-49 sequence, then `finops
    restart`, and requires that at least one of them either succeeds or
    reports a topology that agrees. A state where all three exit non-zero
    and disagree is an operator with no `finops` command that can help —
    the situation ADR-008's one-command-rollback guarantee exists to
    prevent.
    """
    engine = finops_env
    _seed_releases(engine, [("aaaaaaa", "previous", None), ("bbbbbbb", "current", "aaaaaaa")])
    for release_id in ("aaaaaaa", "bbbbbbb"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)

    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_release", _healthy_by_path_broken_via_current)

    first = runner.invoke(finops_module.app, ["rollback", "--release-root", str(tmp_path)])
    retry = runner.invoke(finops_module.app, ["rollback", "--release-root", str(tmp_path)])
    restart = runner.invoke(finops_module.app, ["restart", "--release-root", str(tmp_path)])

    recovered = retry.exit_code == 0 or restart.exit_code == 0
    bookkeeping_current = next(
        (rid for rid, st in _statuses(engine).items() if st == "current"), None
    )
    agrees = bookkeeping_current == read_current_target(tmp_path)

    assert recovered or agrees, (
        "after a rollback whose post-swap probe failed, every `finops` recovery command is "
        f"refused and disk/bookkeeping still disagree: first rollback exited "
        f"{first.exit_code}, retry exited {retry.exit_code}, restart exited "
        f"{restart.exit_code}; symlink -> {read_current_target(tmp_path)!r}, bookkeeping "
        f"current -> {bookkeeping_current!r}"
    )


# ---------------------------------------------------------------------------
# QA-50 — the QA-47 contention guard now refuses *after* the symlink moved.
# ---------------------------------------------------------------------------


def test_contended_rollback_reports_a_refusal_it_did_not_actually_make(
    finops_env, tmp_path, monkeypatch
) -> None:
    """QA-48 fixed `finops rollback` reporting a change it did not make.
    This is the mirror image, introduced by ADR-019's disk-then-bookkeeping
    ordering: a refusal that *did* change production.

    `release_ops.rollback` takes the deploy-promotion advisory lock and
    raises `RollbackContendedError` when it cannot get it — the QA-47 fix,
    and correct in itself. But `_do_rollback` calls it only after
    `repoint_current` and the systemd restart have happened. So with a
    concurrent deploy holding the lock, `finops rollback` prints
    "rollback could not complete safely: a concurrent deploy is promoting
    a release right now; refusing to roll back until it finishes — retry
    once it settles", exits 1, and has nonetheless switched `current` to
    the rollback target behind the operator's back. The concurrent deploy
    it deferred to is now the one whose release is *not* running.

    Deterministic, no timing: a separate session holds
    `_DEPLOY_PROMOTION_LOCK_KEY` in an open transaction for the whole
    invocation — the same technique
    `test_deploy_gate_round5_regression.py::test_rollback_respects_the_deploy_promotion_advisory_lock`
    uses, here driven through the CLI so the filesystem half is included.
    """
    engine = finops_env
    _seed_releases(engine, [("aaaaaaa", "previous", None), ("bbbbbbb", "current", "aaaaaaa")])
    for release_id in ("aaaaaaa", "bbbbbbb"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)

    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_release", _healthy_run_release)

    with Session(engine) as lock_holder:
        lock_holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": release_ops._DEPLOY_PROMOTION_LOCK_KEY},
        )
        result = runner.invoke(finops_module.app, ["rollback", "--release-root", str(tmp_path)])
        lock_holder.rollback()

    assert result.exit_code == 1, result.output
    assert "could not complete safely" in result.output
    assert read_current_target(tmp_path) == "bbbbbbb", (
        "`finops rollback` refused ('retry once it settles') but had already repointed "
        f"`current` to {read_current_target(tmp_path)!r} and restarted the configured units "
        "against it; bookkeeping still records the concurrent deploy's release as current "
        f"({_statuses(engine)})"
    )


# ---------------------------------------------------------------------------
# QA-51 — `finops deploy` exits 0 for a promotion that never happened.
# ---------------------------------------------------------------------------


def test_deploy_does_not_report_success_when_mark_healthy_declined_to_promote(
    finops_env, tmp_path, monkeypatch
) -> None:
    """`mark_healthy` has a documented non-promoting outcome (QA-3): if
    another deploy holds the promotion advisory lock, it records the
    release as `failed` / "concurrent deploy detected while promoting to
    current" and returns normally, promoting nothing. Its docstring
    justifies that as accurate because "its own image was never actually
    the one promoted".

    Under ADR-019 that justification no longer holds, and `deploy`'s
    caller never checked the outcome anyway:

        if health["overall"] == "healthy":
            release_ops.mark_healthy(session, release)
            deployed_ok = True

    `deployed_ok` comes from the health check, not from the promotion. By
    the time this runs, `repoint_current` has already switched `current`
    to the new release and `run_systemctl restart` has already restarted
    the units against it. So the observable outcome is:

    * `finops deploy <sha>` prints "deployed <sha>" and **exits 0** —
      automation, a runbook step, and `finops deploy && ...` all read that
      as success;
    * `ops.releases` records `<sha>` as `failed` / `unhealthy`;
    * `current` resolves to `<sha>` anyway;
    * the ADR-008 auto-rollback branch is never entered, because it is
      gated on `health["overall"]`, not on whether the promotion landed.

    A deploy that exits 0 while its own bookkeeping calls it failed is the
    one outcome a narrow, auditable production interface must never
    produce.
    """
    engine = finops_env
    _seed_releases(engine, [("aaaaaaa", "previous", None), ("bbbbbbb", "current", "aaaaaaa")])
    for release_id in ("aaaaaaa", "bbbbbbb", "ccccccc"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)

    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_release", _healthy_run_release)
    _seed_fresh_backup(engine)

    with Session(engine) as lock_holder:
        lock_holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": release_ops._DEPLOY_PROMOTION_LOCK_KEY},
        )
        result = runner.invoke(
            finops_module.app, ["deploy", "ccccccc", "--release-root", str(tmp_path)]
        )
        lock_holder.rollback()

    statuses = _statuses(engine)
    promoted = statuses.get("ccccccc") == "current"
    assert (result.exit_code == 0) == promoted, (
        f"`finops deploy ccccccc` exited {result.exit_code} saying "
        f"{result.output.strip()!r}, but bookkeeping records {statuses} and `current` "
        f"resolves to {read_current_target(tmp_path)!r} — exit status and the promotion "
        "that actually happened disagree"
    )


# ---------------------------------------------------------------------------
# QA-52 — a failed deploy with no rollback target leaves `current` on it.
# ---------------------------------------------------------------------------


def test_failed_deploy_with_no_rollback_target_does_not_strand_current_on_it(
    finops_env, tmp_path, monkeypatch
) -> None:
    """The sequence:

    1. `bbbbbbb` is `current` and is the first release ever deployed, so
       `replaces_release_id IS NULL`. Real, and the state every new
       production host is in for its first few deploys.
    2. `finops deploy ccccccc` — the release is installed, a backup is on
       record, the migration preflight succeeds, and the by-path probe
       reports healthy. `deploy` therefore repoints `current` to
       `ccccccc` and restarts the units.
    3. The re-probe through `current` — the check that exists to catch
       exactly this — reports unhealthy. `deploy` marks `ccccccc` failed
       and enters the ADR-008 auto-rollback.
    4. `get_previous` returns `None`: the newest attempt is `failed`, so
       it resolves to whatever is `current` (`bbbbbbb`), and `bbbbbbb`
       has no deploy history of its own. `deploy` prints "no previous
       known-good release to roll back to — manual intervention required"
       and exits 1.

    `get_previous`'s reasoning for that `None` is explicit in its
    docstring: "`mark_failed` never touches current/previous ... so
    whatever is tracked as `current` right now is still the release that
    is actually running". Under the Docker model that was true. Under
    ADR-019 it is false the moment `repoint_current` runs, which happens
    before any of this — so the rollback that *is* both possible and
    necessary (put `current` back on `bbbbbbb`, whose directory is right
    there on disk and which just passed nothing but has been running fine)
    is never attempted.

    What the operator is left with: `current` -> `ccccccc`, a release this
    command just declared unhealthy; bookkeeping -> `bbbbbbb`; `finops
    rollback` exits 1 with "No previous known-good release is tracked" and
    changes nothing; `finops restart` exits 1 refusing on the topology
    disagreement. And the message they were given never mentions that
    `current` was moved at all.
    """
    engine = finops_env
    _seed_releases(engine, [("bbbbbbb", "current", None)])
    for release_id in ("bbbbbbb", "ccccccc"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)

    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_release", _healthy_by_path_broken_via_current)
    _seed_fresh_backup(engine)

    result = runner.invoke(
        finops_module.app, ["deploy", "ccccccc", "--release-root", str(tmp_path)]
    )

    assert result.exit_code == 1, result.output
    assert read_current_target(tmp_path) == "bbbbbbb", (
        "a deploy whose post-restart probe failed and which had no rollback target left "
        f"`current` resolving to {read_current_target(tmp_path)!r} — the release it just "
        f"marked failed ({_statuses(engine)}) — and said only "
        f"{result.output.strip()!r}"
    )


def test_failed_first_deploy_leaves_a_recoverable_state(finops_env, tmp_path, monkeypatch) -> None:
    """The recovery half of QA-52, asserted separately so it survives the
    symlink fix: after the QA-52 sequence, some `finops` command must be
    able to get production back to agreement. Currently `rollback` exits 1
    ("No previous known-good release is tracked") and `restart` exits 1
    (topology disagreement), leaving nothing but a hand-edited symlink."""
    engine = finops_env
    _seed_releases(engine, [("bbbbbbb", "current", None)])
    for release_id in ("bbbbbbb", "ccccccc"):
        _make_release_dir(tmp_path, release_id)
    (tmp_path / "current").symlink_to(Path("releases") / "bbbbbbb", target_is_directory=True)

    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_release", _healthy_by_path_broken_via_current)
    _seed_fresh_backup(engine)

    runner.invoke(finops_module.app, ["deploy", "ccccccc", "--release-root", str(tmp_path)])
    rollback_result = runner.invoke(
        finops_module.app, ["rollback", "--release-root", str(tmp_path)]
    )
    restart_result = runner.invoke(finops_module.app, ["restart", "--release-root", str(tmp_path)])

    bookkeeping_current = next(
        (rid for rid, st in _statuses(engine).items() if st == "current"), None
    )
    agrees = bookkeeping_current == read_current_target(tmp_path)

    assert rollback_result.exit_code == 0 or restart_result.exit_code == 0 or agrees, (
        "after a failed first deploy, `finops rollback` exited "
        f"{rollback_result.exit_code} ({rollback_result.output.strip()!r}) and `finops "
        f"restart` exited {restart_result.exit_code}, and disk/bookkeeping still disagree "
        f"(symlink -> {read_current_target(tmp_path)!r}, bookkeeping -> "
        f"{bookkeeping_current!r}) — no `finops` command can recover this host"
    )
