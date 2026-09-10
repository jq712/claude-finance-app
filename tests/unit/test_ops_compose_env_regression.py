"""Adversarial regression tests for `ops/compose.py` (QA, Milestone 7).

Every test here encodes a defect found by adversarial review of the
Milestone 7 deployment branch. They are marked `xfail(strict=True)` so the
suite stays green until the implementation is fixed and then fails loudly
(XPASS) the moment it is — at which point the marker should be deleted,
not the test.

Defect QA-1: `run_compose(..., env={...})` passes its `env` dict straight
through to `subprocess.run(env=...)`, which *replaces* the child
environment rather than extending it. `finops deploy`/`rollback` call it
with `env={"RELEASE_ID": ...}` only, so `docker compose` runs with no
PATH, no HOME (no GHCR registry auth) and none of the
`${FINANCE_*_DB_PASSWORD:?required}` / `${PLAID_*:?required}` variables
`deploy/compose.yaml` interpolates. Reproduced with the real binary:

    env -i PATH=... RELEASE_ID=abc1234 docker compose \\
        -f deploy/compose.yaml config --quiet
    error while interpolating services.app.environment.DATABASE_URL:
      required variable FINANCE_APP_DB_PASSWORD is missing a value
"""

from __future__ import annotations

import os
import subprocess

from finance_app.ops.compose import run_compose


def test_run_compose_extends_the_parent_environment_rather_than_replacing_it() -> None:
    captured: dict[str, dict[str, str]] = {}

    def fake_runner(command, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    os.environ["QA_PARENT_MARKER"] = "must-survive"
    try:
        run_compose(
            "deploy/compose.yaml",
            "up",
            "-d",
            "app",
            env={"RELEASE_ID": "abc1234"},
            runner=fake_runner,
        )
    finally:
        os.environ.pop("QA_PARENT_MARKER", None)

    child_env = captured["env"]
    assert child_env["RELEASE_ID"] == "abc1234"
    # The deploy-specific variable must be *added*, not substituted for the
    # whole environment: PATH, HOME and the credential variables exported by
    # deploy/scripts/with-production-env.sh all have to survive.
    assert child_env.get("QA_PARENT_MARKER") == "must-survive"
    assert "PATH" in child_env


def test_deploy_env_is_sufficient_for_compose_interpolation() -> None:
    """The env dict `finops deploy` builds must, on its own, be enough for
    `docker compose -f deploy/compose.yaml` to interpolate. Today it is a
    single key, so this can never hold — which is the defect."""
    captured: dict[str, dict[str, str]] = {}

    def fake_runner(command, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    run_compose(
        "deploy/compose.yaml", "pull", "app", env={"RELEASE_ID": "abc1234"}, runner=fake_runner
    )

    required = {
        "FINANCE_MIGRATOR_DB_PASSWORD",
        "FINANCE_APP_DB_PASSWORD",
        "FINANCE_AGENT_DB_PASSWORD",
        "FINANCE_OBSERVER_DB_PASSWORD",
        "FINANCE_BACKUP_DB_PASSWORD",
        "PLAID_CLIENT_ID",
        "PLAID_SECRET",
        "PLAID_ACCESS_TOKEN",
    }
    missing = required - set(captured["env"])
    assert not missing, f"docker compose would fail interpolating: {sorted(missing)}"
