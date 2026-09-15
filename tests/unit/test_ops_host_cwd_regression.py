"""Regression test for `ops/host.py:run_release`'s missing `cwd`.

Defect: `run_release` passed no `cwd` to `subprocess.run`, so a release
binary it launched (`alembic`, `finance selfcheck`) inherited whatever
directory the *caller* — `finops`, launched by the operator — happened to
be running from, not the release directory being deployed. Both
`Config("alembic.ini")` (`ops/status.py:migration_status`, its default
`alembic_ini_path` is a plain relative string) and Alembic's own
`%(here)s` interpolation resolve relative to the process's cwd. Deployed
from an operator's own shell (no `alembic.ini` there),
`migration_status` silently degrades to `{"status": "unknown"}`,
`ops/selfcheck.py` folds that into `overall: "unhealthy"`, and a
perfectly good release fails its own deploy health gate and gets rolled
back automatically — for a reason that has nothing to do with the release.
Deployed from a *different* release's directory, `finops.py:452`'s
migration preflight (`alembic upgrade head`) would silently apply the
wrong release's migrations against `finance_prod`.

The fix: `run_release` now defaults `cwd` to `release_path` itself, so
both call sites resolve `alembic.ini`/`migrations/` inside the release
actually under deployment, regardless of where the operator invoked
`finops` from."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from finance_app.ops.host import run_release


def test_run_release_defaults_cwd_to_the_release_path() -> None:
    captured: dict[str, object] = {}

    def fake_runner(command, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    run_release("/opt/finance/releases/abc1234", "alembic", "upgrade", "head", runner=fake_runner)

    assert captured["cwd"] == "/opt/finance/releases/abc1234"


def test_an_explicit_cwd_overrides_the_release_path_default() -> None:
    captured: dict[str, object] = {}

    def fake_runner(command, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    run_release(
        "/opt/finance/releases/abc1234",
        "finance",
        "selfcheck",
        cwd="/some/other/dir",
        runner=fake_runner,
    )

    assert captured["cwd"] == "/some/other/dir"


def test_a_real_child_process_actually_runs_from_the_release_path_not_the_callers_cwd(
    tmp_path: Path,
) -> None:
    """End to end with the real `subprocess.run` (no fake runner): launch a
    `.venv/bin/finance`-shaped shell stub that reports its own working
    directory, invoked while this test process's own cwd is somewhere else
    entirely, and confirm the child actually ran from the release path —
    the exact condition `alembic.ini`/`%(here)s` resolution depends on."""
    release_path = tmp_path / "releases" / "abc1234"
    bin_dir = release_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "finance"
    stub.write_text("#!/bin/sh\npwd\n")
    stub.chmod(0o755)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    original_cwd = os.getcwd()
    os.chdir(elsewhere)
    try:
        result = run_release(release_path, "finance", "selfcheck")
    finally:
        os.chdir(original_cwd)

    assert result.stdout.strip() == str(release_path.resolve())
