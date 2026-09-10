"""Unit tests for the `docker compose` wrapper behind `finops restart`/
`deploy`/`rollback` (`src/finance_app/ops/compose.py`). The `runner` is
injected so these run with no Docker daemon at all."""

import subprocess

import pytest

from finance_app.ops.compose import ComposeError, compose_command, run_compose


def test_compose_command_builds_expected_argv() -> None:
    assert compose_command("deploy/compose.yaml", "up", "-d", "app") == [
        "docker",
        "compose",
        "-f",
        "deploy/compose.yaml",
        "up",
        "-d",
        "app",
    ]


def test_run_compose_invokes_the_runner_with_the_built_command() -> None:
    calls = []

    def fake_runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run_compose(
        "deploy/compose.yaml", "restart", "app", env={"RELEASE_ID": "abc123"}, runner=fake_runner
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["docker", "compose", "-f", "deploy/compose.yaml", "restart", "app"]
    # `env` is merged onto the parent process environment, not substituted
    # for it (QA-1 regression, tests/unit/test_ops_compose_env_regression.py)
    # — the deploy-specific overlay must be present, but so must everything
    # else the parent process already had (PATH, credentials exported by
    # deploy/scripts/with-production-env.sh, ...).
    assert kwargs["env"]["RELEASE_ID"] == "abc123"
    assert "PATH" in kwargs["env"]


def test_run_compose_wraps_a_nonzero_exit_as_compose_error() -> None:
    def failing_runner(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=command, output="", stderr="service 'app' failed to start"
        )

    with pytest.raises(ComposeError, match="service 'app' failed to start"):
        run_compose("deploy/compose.yaml", "up", "-d", runner=failing_runner)


def test_run_compose_reports_a_clear_error_when_docker_is_missing() -> None:
    def missing_docker_runner(command, **kwargs):
        raise FileNotFoundError("docker")

    with pytest.raises(ComposeError, match="docker CLI not found"):
        run_compose("deploy/compose.yaml", "ps", runner=missing_docker_runner)
