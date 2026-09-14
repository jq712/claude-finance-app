"""Unit tests for the bare-metal release-tree/`systemctl` wrapper behind
`finops restart`/`deploy`/`rollback` (`src/finance_app/ops/host.py`,
superseding the Docker-based `ops/compose.py` under ADR-019). The `runner`
is injected so these run with no production host, root, or `/opt/finance`
at all — the same seam `ops/compose.py`'s `run_compose` used, retargeted
rather than rewritten (see `test_ops_compose.py`'s prior version for the
1:1 correspondence)."""

import subprocess

import pytest

from finance_app.ops.host import (
    HostCommandError,
    current_link,
    read_current_target,
    release_command,
    release_dir,
    release_is_installed,
    repoint_current,
    run_release,
    run_systemctl,
)


def test_release_command_builds_expected_argv() -> None:
    assert release_command("/opt/finance/releases/abc123", "finance", "selfcheck", "--json") == [
        "/opt/finance/releases/abc123/.venv/bin/finance",
        "selfcheck",
        "--json",
    ]


def test_run_release_invokes_the_runner_with_the_built_command() -> None:
    calls = []

    def fake_runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run_release(
        "/opt/finance/releases/abc123",
        "finance",
        "selfcheck",
        env={"RELEASE_ID": "abc123"},
        runner=fake_runner,
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["/opt/finance/releases/abc123/.venv/bin/finance", "selfcheck"]
    # `env` is merged onto the parent process environment, not substituted
    # for it (QA-1 regression, tests/unit/test_ops_host_env_regression.py)
    # — the deploy-specific overlay must be present, but so must everything
    # else the parent process already had (PATH, FINANCE_ENV_FILE, ...).
    assert kwargs["env"]["RELEASE_ID"] == "abc123"
    assert "PATH" in kwargs["env"]


def test_run_release_wraps_a_nonzero_exit_as_host_command_error() -> None:
    def failing_runner(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=command, output="", stderr="release failed to start"
        )

    with pytest.raises(HostCommandError, match="release failed to start"):
        run_release("/opt/finance/releases/abc123", "finance", "selfcheck", runner=failing_runner)


def test_run_release_preserves_stdout_on_a_nonzero_exit() -> None:
    """`HostCommandError.stdout` is load-bearing: `probe_release` parses a
    selfcheck JSON payload out of a *failed* run's stdout, since a
    self-reported-unhealthy selfcheck exits 1."""

    def failing_runner(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=command, output='{"overall": "unhealthy"}', stderr="boom"
        )

    with pytest.raises(HostCommandError) as exc_info:
        run_release("/opt/finance/releases/abc123", "finance", "selfcheck", runner=failing_runner)
    assert exc_info.value.stdout == '{"overall": "unhealthy"}'


def test_run_release_reports_a_clear_error_when_the_venv_binary_is_missing() -> None:
    def missing_binary_runner(command, **kwargs):
        raise FileNotFoundError()

    with pytest.raises(HostCommandError, match="not found"):
        run_release(
            "/opt/finance/releases/abc123", "finance", "selfcheck", runner=missing_binary_runner
        )


def test_run_systemctl_rejects_a_malformed_unit_name_before_spawning_anything() -> None:
    calls = []

    def recording_runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(HostCommandError, match="does not look like a systemd unit name"):
        run_systemctl("restart", "finance-app.service; rm -rf /", runner=recording_runner)
    assert calls == []


def test_run_systemctl_builds_expected_argv_with_a_custom_prefix() -> None:
    calls = []

    def recording_runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run_systemctl(
        "restart",
        "finance-app.service",
        "finance-sync.timer",
        prefix=("sudo", "-n", "systemctl"),
        runner=recording_runner,
    )
    assert calls == [
        ["sudo", "-n", "systemctl", "restart", "finance-app.service", "finance-sync.timer"]
    ]


def test_release_is_installed_requires_a_real_directory_with_a_built_venv(tmp_path) -> None:
    root = tmp_path
    installed = root / "releases" / "aaa1111"
    (installed / ".venv" / "bin").mkdir(parents=True)
    (installed / ".venv" / "bin" / "finance").touch()

    assert release_is_installed(root, "aaa1111") is True
    assert release_is_installed(root, "bbb2222") is False  # doesn't exist at all


def test_release_is_installed_rejects_a_symlinked_release_directory(tmp_path) -> None:
    root = tmp_path
    real = root / "releases" / "aaa1111"
    (real / ".venv" / "bin").mkdir(parents=True)
    (real / ".venv" / "bin" / "finance").touch()
    (root / "releases" / "bbb2222").symlink_to(real, target_is_directory=True)

    assert release_is_installed(root, "bbb2222") is False


def test_repoint_current_is_atomic_and_reports_the_prior_target(tmp_path) -> None:
    root = tmp_path
    for release_id in ("aaa1111", "bbb2222"):
        d = root / "releases" / release_id / ".venv" / "bin"
        d.mkdir(parents=True)
        (d / "finance").touch()

    assert read_current_target(root) is None

    previous = repoint_current(root, "aaa1111")
    assert previous is None
    assert read_current_target(root) == "aaa1111"
    assert current_link(root).is_symlink()

    previous = repoint_current(root, "bbb2222")
    assert previous == "aaa1111"
    assert read_current_target(root) == "bbb2222"


def test_repoint_current_refuses_a_missing_release_directory(tmp_path) -> None:
    with pytest.raises(HostCommandError, match="does not exist"):
        repoint_current(tmp_path, "nonexistent")


def test_repoint_current_refuses_when_current_is_a_real_directory_not_a_symlink(tmp_path) -> None:
    root = tmp_path
    d = root / "releases" / "aaa1111" / ".venv" / "bin"
    d.mkdir(parents=True)
    (d / "finance").touch()
    (root / "current").mkdir()  # a real directory, not a symlink

    with pytest.raises(HostCommandError, match="not a symlink"):
        repoint_current(root, "aaa1111")


def test_release_dir_and_current_link_paths(tmp_path) -> None:
    assert release_dir(tmp_path, "aaa1111") == tmp_path / "releases" / "aaa1111"
    assert current_link(tmp_path) == tmp_path / "current"
