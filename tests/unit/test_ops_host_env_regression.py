"""Adversarial regression test for `ops/host.py` (QA-1, carried forward
from the superseded `ops/compose.py` under ADR-019).

Defect QA-1: `run_release(..., env={...})` passes its `env` dict straight
through to `subprocess.run(env=...)`, which *replaces* the child
environment rather than extending it. `finops deploy`/`rollback` call it
with `env={"RELEASE_ID": ...}` only, so the release binary would run with
no PATH, no HOME, and — critically under ADR-019 — no `FINANCE_ENV_FILE`,
which is how a release resolves `/opt/finance/.env` instead of falling
back to dev defaults (`config/env.py`).

The old `docker compose`-interpolation counterpart of this test
(`test_deploy_env_is_sufficient_for_compose_interpolation`) is retired,
not retargeted: there is no interpolation step in the bare-metal model.
What replaces it below is the ADR-019-shaped equivalent — the one
environment variable that actually matters for correctness under this
substrate (`FINANCE_ENV_FILE`) must survive the same overlay, and no
credential is ever passed as a CLI argument (every DSN is read from the
env file inside the child process, never from argv)."""

from __future__ import annotations

import os
import subprocess

from finance_app.ops.host import run_release


def test_run_release_extends_the_parent_environment_rather_than_replacing_it() -> None:
    captured: dict[str, dict[str, str]] = {}

    def fake_runner(command, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    os.environ["QA_PARENT_MARKER"] = "must-survive"
    try:
        run_release(
            "/opt/finance/releases/abc1234",
            "finance",
            "selfcheck",
            env={"RELEASE_ID": "abc1234"},
            runner=fake_runner,
        )
    finally:
        os.environ.pop("QA_PARENT_MARKER", None)

    child_env = captured["env"]
    assert child_env["RELEASE_ID"] == "abc1234"
    # The deploy-specific variable must be *added*, not substituted for the
    # whole environment: PATH, HOME, and FINANCE_ENV_FILE (how the child
    # resolves /opt/finance/.env instead of falling back to dev defaults)
    # all have to survive.
    assert child_env.get("QA_PARENT_MARKER") == "must-survive"
    assert "PATH" in child_env


def test_run_release_never_needs_a_credential_passed_as_an_argument() -> None:
    """No DSN, password, or API key is ever part of the argv `run_release`
    builds — every credential the release binary needs comes from the env
    file it resolves for itself (`FINANCE_ENV_FILE`, inherited via the
    overlay above), never from a CLI argument this process constructs.
    A credential in argv would be visible in `ps`/process listings on a
    shared host; a credential in the inherited environment is not."""
    from finance_app.ops.host import release_command

    argv = release_command("/opt/finance/releases/abc1234", "finance", "selfcheck", "--json")
    for token in argv:
        assert "password" not in token.lower()
        assert "://" not in token  # no DSN-shaped string
        assert not token.startswith("sk-")  # no bare API key shape


def test_run_release_preserves_finance_env_file_through_the_overlay() -> None:
    """`FINANCE_ENV_FILE` (config/env.py) is exactly the kind of ambient
    environment variable QA-1's overlay must not drop — a release process
    started without it silently falls back to dev DSN defaults instead of
    refusing (config/settings.py's own guard only fires when a DSN
    actually names finance_prod; if the release never even sees the env
    var, it never gets the chance)."""
    captured: dict[str, dict[str, str]] = {}

    def fake_runner(command, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    os.environ["FINANCE_ENV_FILE"] = "/opt/finance/.env"
    try:
        run_release(
            "/opt/finance/releases/abc1234",
            "alembic",
            "upgrade",
            "head",
            runner=fake_runner,
        )
    finally:
        os.environ.pop("FINANCE_ENV_FILE", None)

    assert captured["env"].get("FINANCE_ENV_FILE") == "/opt/finance/.env"
