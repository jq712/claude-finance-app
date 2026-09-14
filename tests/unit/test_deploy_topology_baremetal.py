"""Adversarial regression tests for `ops/status.py:probe_release`
(originally ADR-016 D3, retargeted from the superseded Docker Compose
model to ADR-019's bare-metal release directories).

Moved out of `test_deploy_topology_regression.py`: that file's remaining
subject matter (`deploy/compose.yaml`, `deploy/scripts/
with-production-env.sh`, the restore-drill unit) is unrelated to
`probe_release` and stays untouched — those files are still present and
out of scope for this change; they get deleted together in a later PR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from finance_app.ops.host import HostCommandError, run_release
from finance_app.ops.identity import installed_release_id, installed_release_root
from finance_app.ops.status import probe_release

_REPO_ROOT = Path(__file__).resolve().parents[2]

_UNHEALTHY_PAYLOAD = json.dumps(
    {
        "release_id": "abc1234",
        "installed_release_id": "abc1234",
        "app_version": "0.1.0",
        "database": {"status": "healthy"},
        "migrations": {"status": "drift", "applied": "aaa", "head": "bbb"},
        "overall": "unhealthy",
    }
)
_HEALTHY_LOOKALIKE = json.dumps(
    {"logger": "finance_app.ops", "release_id": "abc1234", "overall": "healthy"}
)


def _runner_returning(stdout: str):  # noqa: ANN202
    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0, stdout, "")

    return fake_runner


def test_probe_release_reports_healthy_when_the_right_release_selfchecks_clean() -> None:
    payload = json.dumps(
        {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "healthy"}
    )

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0, payload + "\n", "")

    result = probe_release(release_id="abc1234", run_release_fn=fake_runner)
    assert result["status"] == "healthy"
    assert result["reported_release_id"] == "abc1234"


_HEALTHY_ABC1234 = json.dumps(
    {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "healthy"}
)


@pytest.mark.parametrize(
    "stdout",
    [
        _HEALTHY_ABC1234 + "\n",
        "Starting up...\n" + _HEALTHY_ABC1234,
        _HEALTHY_ABC1234 + "\nprocess exited cleanly\n",
        _HEALTHY_ABC1234 + '\n{"level": "info", "msg": "done"}\n',
    ],
    ids=["clean", "leading-chatter", "trailing-chatter", "trailing-unrelated-json"],
)
def test_probe_release_finds_the_payload_around_surrounding_noise(stdout: str) -> None:
    """`_parse_selfcheck_stdout` must not stop at the first line that
    fails to parse or isn't the selfcheck payload — a version that does
    would misreport a genuinely healthy release as `unreachable` (from
    startup/log chatter sharing the same stdout) or `wrong_release` (from
    an unrelated JSON object elsewhere in the stream), which
    `deploy_health_check` would then auto-rollback (ADR-008) — exactly
    the QA-2 failure class the surrounding code exists to prevent."""

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0, stdout, "")

    result = probe_release(release_id="abc1234", run_release_fn=fake_runner)
    assert result["status"] == "healthy", result
    assert result["reported_release_id"] == "abc1234"


def test_probe_release_detects_a_wrong_release() -> None:
    """QA-26's underlying concern — a probe that could report healthy for
    the wrong release — is structurally eliminated by D3 (carried forward
    under ADR-019): `probe_release` runs the exact release directory
    under deployment and checks what it reports about itself, so a
    stale/wrong release is caught by content, not by luck."""
    # `release_id` (the RELEASE_ID env var probe_release injected) reads
    # back as the requested id, same as ever — it's `installed_release_id`
    # (the identity read from the RELEASE_ID file inside the release tree
    # actually running, QA-37) that disagrees, which is what
    # `wrong_release` must catch.
    payload = json.dumps(
        {"release_id": "abc1234", "installed_release_id": "stale999", "overall": "healthy"}
    )

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0, payload + "\n", "")

    result = probe_release(release_id="abc1234", run_release_fn=fake_runner)
    assert result["status"] == "wrong_release"
    assert result["reported_release_id"] == "stale999"


def test_probe_release_reports_unhealthy_on_a_nonzero_selfcheck_exit() -> None:
    payload = json.dumps(
        {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "unhealthy"}
    )

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise HostCommandError(
            "/opt/finance/releases/abc1234/.venv/bin/finance selfcheck failed (exit 1): ...",
            stdout=payload + "\n",
        )

    result = probe_release(release_id="abc1234", run_release_fn=fake_runner)
    assert result["status"] == "unhealthy"
    assert result["reported_release_id"] == "abc1234"


def test_probe_release_reports_unreachable_when_the_release_binary_itself_fails() -> None:
    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise HostCommandError("finance binary not found")

    result = probe_release(release_id="abc1234", run_release_fn=fake_runner)
    assert result["status"] == "unreachable"
    assert result["reported_release_id"] is None


def test_probe_release_invokes_the_release_by_path_by_default() -> None:
    """`via_current=False` (the default) must probe the release by its
    own path, not through `current` — this is what lets a *new* release be
    verified before `current` is ever repointed to it."""
    captured = {}

    def fake_runner(release_path, *args, **kwargs):  # noqa: ANN002, ANN003
        captured["release_path"] = str(release_path)
        payload = json.dumps(
            {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "healthy"}
        )
        return subprocess.CompletedProcess([], 0, payload, "")

    probe_release(release_id="abc1234", release_root="/opt/finance", run_release_fn=fake_runner)
    assert captured["release_path"] == "/opt/finance/releases/abc1234"


def test_probe_release_invokes_through_current_when_requested() -> None:
    """`via_current=True` probes through the `current` symlink itself —
    used for the post-restart re-check, where the whole point is to
    confirm what `current` actually resolves to right now, not what a
    specific release directory reports in isolation."""
    captured = {}

    def fake_runner(release_path, *args, **kwargs):  # noqa: ANN002, ANN003
        captured["release_path"] = str(release_path)
        payload = json.dumps(
            {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "healthy"}
        )
        return subprocess.CompletedProcess([], 0, payload, "")

    probe_release(
        release_id="abc1234",
        release_root="/opt/finance",
        via_current=True,
        run_release_fn=fake_runner,
    )
    assert captured["release_path"] == "/opt/finance/current"


@pytest.mark.parametrize(
    "stdout",
    [
        _UNHEALTHY_PAYLOAD + "\n" + _HEALTHY_LOOKALIKE + "\n",
        _UNHEALTHY_PAYLOAD + "\n" + json.dumps({"release_id": "abc1234", "overall": "healthy"}),
    ],
    ids=["log-shaped-lookalike", "bare-lookalike"],
)
def test_probe_release_does_not_let_a_later_lookalike_override_the_real_payload(
    stdout: str,
) -> None:
    """`_parse_selfcheck_stdout` requires both `release_id` and `overall`
    before accepting a line, which stops log chatter and unrelated JSON
    from being *mistaken* for the payload — the false-negative direction
    (a healthy release reported `unreachable`/`wrong_release`, then
    auto-rolled back).

    The false-positive direction is what this test actually pins: the
    scan must not return the *last* matching line and let anything later
    in the stream that happens to carry both keys silently win over the
    selfcheck's own output. `configure_logging` installs a JSON
    `StreamHandler` on **stdout** (`ops/logging.py`), and a release
    process runs with `LOG_FORMAT: json`, so its own log stream shares
    this file descriptor with the payload this parser trusts. Every
    string in that stream is attacker-influenceable per CLAUDE.md (Plaid
    merchant text, model responses, `ops.errors.message`). A
    deterministic gate must not resolve "which of these is the real
    payload" by position."""
    result = probe_release(release_id="abc1234", run_release_fn=_runner_returning(stdout))
    assert result["status"] != "healthy", (
        f"a lookalike JSON line overrode an `overall: unhealthy` payload: {result}"
    )


def test_probe_release_fails_closed_when_two_candidate_payloads_disagree() -> None:
    """Same root cause, stated as the invariant that matters: if the stream
    contains more than one thing claiming to be the selfcheck payload and
    they do not agree, the deploy gate has no basis for picking one and
    must fail closed."""
    healthy = json.dumps(
        {"release_id": "abc1234", "installed_release_id": "abc1234", "overall": "healthy"}
    )
    both_orders = [f"{healthy}\n{_UNHEALTHY_PAYLOAD}\n", f"{_UNHEALTHY_PAYLOAD}\n{healthy}\n"]
    verdicts = {
        probe_release(release_id="abc1234", run_release_fn=_runner_returning(s))["status"]
        for s in both_orders
    }
    assert verdicts == {"unhealthy"}, (
        f"the verdict depends on which conflicting payload came last: {verdicts}"
    )


def test_probe_release_never_reports_healthy_from_a_failed_run() -> None:
    """The `HostCommandError` branch must never produce `healthy`, whatever
    the salvaged stdout says — a selfcheck process that exited non-zero is
    not a healthy release even if some line on its stdout claims
    otherwise."""
    payload = json.dumps({"release_id": "abc1234", "overall": "healthy"})

    def failing(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise HostCommandError("exit 1", stdout=payload + "\n")

    assert probe_release(release_id="abc1234", run_release_fn=failing)["status"] != "healthy"


@pytest.mark.parametrize(
    "raised",
    [
        PermissionError(13, "Permission denied", "finance"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        OSError(12, "Cannot allocate memory"),
    ],
    ids=["permission-denied", "invalid-utf8-on-stdout", "fork-failure"],
)
def test_run_release_wraps_every_subprocess_failure_as_a_host_command_error(
    raised: Exception,
) -> None:
    r"""`ops/host.py` documents itself as the single boundary that turns a
    release-binary invocation failure into `HostCommandError`, and every
    caller in `cli/finops.py` and `ops/status.py` is written to that
    contract (`probe_release` catches only `HostCommandError`; `deploy`
    catches only `HostCommandError`).

    Three realistic failures break it if left unhandled:

    * `PermissionError` — the release binary isn't executable by this
      user;
    * `UnicodeDecodeError` — `subprocess.run(text=True)` decodes strictly,
      so *one* non-UTF-8 byte anywhere on the release's stdout
      (`/bin/sh -c "printf 'x\377y'"` reproduces it) raises instead of
      returning;
    * a bare `OSError` from `fork`/`posix_spawn`.

    Each would otherwise escape as-is, past `probe_release`'s handler and
    past `deploy`'s, aborting `finops deploy` with a raw traceback *after*
    the migration preflight has already run."""

    def exploding_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise raised

    with pytest.raises(HostCommandError):
        run_release("/opt/finance/releases/abc1234", "finance", "ps", runner=exploding_runner)


def test_wrong_release_detection_is_not_a_tautology() -> None:
    """`probe_release`'s `wrong_release` verdict must compare against an
    identity that originates *inside* the release tree, never against
    something the caller (or the release's own environment) supplies —
    otherwise the check can never disagree with itself (the original
    QA-37 bug, under the Docker model).

    Executed end to end rather than grepped: `git archive` a real commit
    of this repository into a throwaway directory (exactly the mechanism
    `docs/runbooks/deploy.md` names for the release-copy step) and confirm
    `ops/identity.py` reads back that commit's SHA from inside the
    archived tree — regardless of what `RELEASE_ID` environment variable
    a caller injects around it.

    Uses `git stash create` to get a real commit-ish covering whatever is
    currently staged/modified (falling back to `HEAD` on a clean tree,
    e.g. once this change has landed and CI runs against a committed
    `RELEASE_ID`) rather than requiring `RELEASE_ID` to already be
    committed at test-authoring time — `git stash create` never touches
    the working tree or the index, it only builds and returns a commit
    object."""
    stash = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "stash", "create"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    commit_ish = stash.stdout.strip()
    if not commit_ish:  # clean tree — nothing to stash, use HEAD directly
        commit_ish = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    tmp_dir = tempfile.mkdtemp()
    try:
        archive = subprocess.run(
            ["git", "archive", commit_ish], cwd=_REPO_ROOT, stdout=subprocess.PIPE, check=True
        )
        subprocess.run(["tar", "-x", "-C", tmp_dir], input=archive.stdout, check=True)

        identity_file = Path(tmp_dir) / "RELEASE_ID"
        assert identity_file.is_file(), "git archive did not produce a RELEASE_ID file at all"
        archived_identity = identity_file.read_text().strip()
        assert archived_identity == commit_ish, (
            "git archive's export-subst did not substitute the real commit SHA into "
            f"RELEASE_ID (got {archived_identity!r}, expected {commit_ish!r}) — "
            "check .gitattributes' `RELEASE_ID export-subst` line"
        )

        # The identity comparison must not be forgeable by whoever starts
        # the process: injecting an attacker-chosen RELEASE_ID into the
        # environment must not change what gets read back.
        fake_binary_dir = Path(tmp_dir) / ".venv" / "bin"
        fake_binary_dir.mkdir(parents=True)
        (fake_binary_dir / "finance").touch()
        original_argv0 = sys.argv[0]
        try:
            sys.argv[0] = str(fake_binary_dir / "finance")
            os.environ["RELEASE_ID"] = "attacker-chosen-value-not-a-real-sha"
            reported = installed_release_id()
        finally:
            sys.argv[0] = original_argv0
            os.environ.pop("RELEASE_ID", None)

        assert reported == commit_ish, (
            f"installed_release_id() reported {reported!r} instead of the real commit "
            f"{commit_ish!r} — it must read the RELEASE_ID file inside the tree, never "
            "an environment variable the caller controls"
        )
    finally:
        shutil.rmtree(tmp_dir)


def test_a_dev_checkout_reports_no_identity_at_all() -> None:
    """A plain checkout (this dev tree, `git worktree add`) never has its
    `RELEASE_ID` file substituted by `git archive` — it must report `None`
    (fail closed), never impersonate a real release by falling through to
    some other value."""
    from finance_app.ops.identity import read_release_identity

    assert read_release_identity(_REPO_ROOT) is None, (
        "this dev tree's own RELEASE_ID file reads back as a real-looking SHA — "
        "either it was committed with a real value instead of the $Format:%H$ "
        "placeholder, or read_release_identity's placeholder check is broken"
    )


def test_installed_release_root_walks_up_from_argv0_not_from_package_location() -> None:
    """Deliberately not derived from `finance_app.__file__`: that resolves
    into `.venv/lib/python3.*/site-packages/finance_app/` once a release
    is built with `uv sync --locked --no-dev` (a non-editable install),
    nowhere near the release root. `sys.argv[0]` is exactly the path
    `ops/host.py:run_release` constructs to invoke the process, so walking
    up from it is correct regardless of how the package was installed."""
    import sys

    original_argv0 = sys.argv[0]
    try:
        sys.argv[0] = "/opt/finance/releases/abc1234/.venv/bin/finance"
        assert installed_release_root() == Path("/opt/finance/releases/abc1234")
    finally:
        sys.argv[0] = original_argv0
