"""Adversarial regression tests for ADR-016's *round-2 fixes* themselves
(QA round 4, Milestone 7 — handoff §19/§20, ADR-008, ADR-016).

Round 2 fixed four things: the `migrate` compose command (ADR-019 removed
`deploy/compose.yaml`/`Dockerfile` entirely, taking this file's former
migrate-command and compose-vs-wrapper drift tests with them — nothing
about the *service command* survives to test once there is no image or
compose service to run it in), `_do_rollback`'s unverified rollback
target, `_parse_selfcheck_stdout`'s sentinel-key guard, and
`with-production-env.sh`'s carriage-return rejection. Round 4 attacked
the surviving fixes directly and found that three of them stop one
variant of the failure while leaving a neighbouring variant wide open,
and that several of the tests written to cover them are structurally
unable to fail:

* `with-production-env.sh` rejects CR and LF but silently *strips* NUL
  bytes and silently keeps leading/trailing spaces — the same "credential
  differs from what was recorded out-of-band, discovered only at restore
  time" corruption the CR check exists to prevent.
* `_parse_selfcheck_stdout` requiring both sentinel keys only closes the
  false-*negative* direction (healthy release misread as broken). The
  false-*positive* direction — moved to `test_deploy_topology_baremetal.py`
  under ADR-019, since it's unrelated to this file's remaining subject
  matter — is still open there too: the *last* line carrying both keys
  wins, so any later lookalike object overrides the real payload.
* `probe_release`'s wrong-release verdict was a tautology as originally
  deployed (also moved: `test_wrong_image_detection_is_not_a_tautology`,
  now rewritten under a new name in `test_deploy_topology_baremetal.py`
  to exercise the fix rather than restate the defect).
* the release-runner error funnel (`ops/compose.py`'s `run_compose`
  originally; `ops/host.py`'s `run_release` under ADR-019 — also moved).
* `restore-verify.sh` cleans up on `INT`/`TERM` — and then keeps running,
  having just deleted its own scratch container and password file.

Defect tests are `xfail(strict=True)`, per the convention in
`tests/integration/test_deploy_gate_regression.py`: delete the marker once
fixed, never the test. Tests that pass are new guards for behavior that was
correct but unpinned.

No Docker, no systemd, no real credentials: the shell tests execute the
real scripts under `/bin/sh` against synthetic fixtures.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from finance_app.ops.status import _parse_selfcheck_stdout
from tests.unit.test_deploy_topology_regression import (
    _CREDENTIAL_FILES,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO_ROOT / "deploy" / "scripts" / "with-production-env.sh"
_RESTORE_VERIFY_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "restore-verify.sh"


def _run_wrapper_with_backup_key(tmp_path: Path, key_bytes: bytes) -> subprocess.CompletedProcess:
    """Run the real wrapper for the `backup` job with `backup_encryption_key`
    set to exactly `key_bytes`, and have the child print the value it
    actually received (hex-encoded, so no byte is lost in transit and no
    credential-shaped string ever lands in test output verbatim)."""
    creds = tmp_path / "creds"
    creds.mkdir(exist_ok=True)
    (creds / "finance_app_db_password").write_bytes(b"apppw")
    (creds / "finance_backup_db_password").write_bytes(b"bkpw")
    (creds / "backup_encryption_key").write_bytes(key_bytes)
    return subprocess.run(
        [
            "/bin/sh",
            str(_WRAPPER),
            "backup",
            "--",
            "/bin/sh",
            "-c",
            'printf "%s" "$BACKUP_ENCRYPTION_KEY" | od -An -tx1 | tr -d " \\n"',
        ],
        env={**os.environ, "CREDENTIALS_DIRECTORY": str(creds)},
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# with-production-env.sh — round 2 fixed CR; NUL and edge whitespace remain.
# ---------------------------------------------------------------------------


def test_wrapper_rejects_a_credential_containing_a_null_byte(tmp_path: Path) -> None:
    r"""`value=$(cat "$file")` drops NUL bytes outright (POSIX command
    substitution has no way to carry one), so `b"good\x00key"` on disk
    becomes `goodkey` in the environment — a *different* passphrase, with
    no error, no warning, and exit 0.

    For `BACKUP_ENCRYPTION_KEY` that means backups get encrypted under a
    passphrase that is not the one the owner recorded out-of-band, and the
    divergence is discovered only at restore time — identical in
    consequence to the trailing-`\r` case round 2 rejected, and reached by
    a credential source (a binary-safe secrets manager, `dd`, an editor
    that pads) that is no more exotic than the CRLF one.

    Must be rejected outright, exactly like the newline and carriage-return
    cases, rather than silently rewritten.
    """
    result = _run_wrapper_with_backup_key(tmp_path, b"good\x00key")
    assert result.returncode != 0, (
        "wrapper exported a NUL-containing credential; the child received "
        f"0x{result.stdout.strip()} for an on-disk value of 0x676f6f6400 6b6579"
    )
    assert "null byte" in result.stderr.lower() or "nul" in result.stderr.lower()


@pytest.mark.parametrize(
    "raw",
    [b"goodkey ", b"goodkey\t", b" goodkey"],
    ids=["trailing-space", "trailing-tab", "leading-space"],
)
def test_wrapper_rejects_a_credential_with_edge_whitespace(tmp_path: Path, raw: bytes) -> None:
    """The wrapper's own comment justifies rejecting whitespace-only values
    as catching "a single stray keystroke while minting a credential". A
    single stray keystroke at the *end* of a real value is the same
    keystroke and the same silent corruption, and passes today."""
    result = _run_wrapper_with_backup_key(tmp_path, raw)
    assert result.returncode != 0, (
        f"wrapper exported {raw!r} unchanged (child received 0x{result.stdout.strip()})"
    )


def test_wrapper_rejects_an_unreadable_credential_file(tmp_path: Path) -> None:
    """A credential file that exists but cannot be read must fail closed,
    not export an empty value. (`set -e` on the `$(cat ...)` assignment
    already does this — pinned here because the export loop's
    `[ -f "$file" ] || continue` makes "absent" a silent skip, and it
    would be an easy edit to make "unreadable" one too.)"""
    creds = tmp_path / "creds"
    creds.mkdir()
    secret = creds / "finance_app_db_password"
    secret.write_bytes(b"apppw")
    secret.chmod(0o000)
    try:
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "health", "--", "/bin/sh", "-c", "echo EXECED"],
            env={**os.environ, "CREDENTIALS_DIRECTORY": str(creds)},
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        secret.chmod(0o600)
    assert result.returncode != 0
    assert "EXECED" not in result.stdout


@pytest.mark.parametrize(
    "raw",
    [b"s3cr3t-value\nsecond-line", b"s3cr3t-value\r", b"   ", b""],
    ids=["embedded-newline", "carriage-return", "whitespace-only", "empty"],
)
def test_wrapper_rejection_messages_never_echo_the_credential_value(
    tmp_path: Path, raw: bytes
) -> None:
    """ "Never echoes a credential value" is the wrapper's own stated
    contract, and every rejection path added since round 2 is a new place
    to break it — a validator that reports *what* it rejected is the
    natural way to write these, and would put a production credential into
    `journalctl -u finance-backup` permanently (CLAUDE.md: logs never carry
    secrets).

    Covers every rejecting branch at once: each must name the credential
    *stem* and the job, never the bytes."""
    result = _run_wrapper_with_backup_key(tmp_path, raw)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "s3cr3t-value" not in combined, f"wrapper echoed the credential value: {combined!r}"
    assert "second-line" not in combined
    assert "backup_encryption_key" in combined or "BACKUP_ENCRYPTION_KEY" in combined, (
        "a rejection must still name which credential failed, or the operator cannot act "
        f"on it: {combined!r}"
    )


# ---------------------------------------------------------------------------
# _parse_selfcheck_stdout — round-2 fix (c) closes only one direction.
# ---------------------------------------------------------------------------

_UNHEALTHY_PAYLOAD = json.dumps(
    {
        "release_id": "abc1234",
        "image_release_id": "abc1234",
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


def test_parse_selfcheck_stdout_still_rejects_non_payload_shapes() -> None:
    """Guards the half of the round-2 fix that does hold, so a future
    rewrite addressing the two xfails above cannot regress it."""
    assert _parse_selfcheck_stdout("") is None
    assert _parse_selfcheck_stdout("   \n\t\n") is None
    assert _parse_selfcheck_stdout('[{"release_id": "a", "overall": "healthy"}]') is None
    assert _parse_selfcheck_stdout('{"context": {"release_id": "a", "overall": "healthy"}}') is None
    assert _parse_selfcheck_stdout('{"release_id": "a"}\n{"overall": "healthy"}') is None
    assert _parse_selfcheck_stdout('42\nnull\n"a string"') is None
    # A payload glued to Compose chatter on one line is not parseable and
    # must not be half-read into a verdict.
    assert (
        _parse_selfcheck_stdout('Container x Created{"release_id": "a", "overall": "healthy"}')
        is None
    )


# ---------------------------------------------------------------------------
# restore-verify.sh — round-2 fix (d): cleanup on every exit path.
# ---------------------------------------------------------------------------


def _fake_bin(tmp_path: Path, *, ready: bool, sleep_seconds: str) -> Path:
    """`docker` and `sleep` stand-ins on `PATH`.

    `docker`: `compose ... backup latest` yields one synthetic backup row,
    `exec ... pg_isready` never succeeds when `ready=False`, everything
    else exits 0 — no daemon, no containers, nothing pulled.

    `sleep`: shortens the readiness loop's own `sleep 2` so the 30-iteration
    timeout path costs milliseconds instead of a minute. It shadows only
    wall-clock, never a branch: the script's control flow, its trap, and
    its cleanup all run exactly as written.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    ready_exit = "0" if ready else "1"
    docker.write_text(
        "#!/bin/sh\n"
        'echo "docker $*" >> "$QA_DOCKER_LOG"\n'
        'case "$1" in\n'
        "  compose)\n"
        '    case "$*" in\n'
        "      *'backup latest'*) printf '7\\t/backups/x.sql.gpg\\n'; exit 0 ;;\n"
        "      *) exit 0 ;;\n"
        "    esac ;;\n"
        f"  exec) exit {ready_exit} ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    sleep_shim = bindir / "sleep"
    sleep_shim.write_text(f"#!/bin/sh\nexec /bin/sleep {sleep_seconds}\n")
    sleep_shim.chmod(0o755)
    return bindir


def _start_restore_verify(
    tmp_path: Path, *, ready: bool, sleep_seconds: str = "0"
) -> subprocess.Popen[bytes]:
    bindir = _fake_bin(tmp_path, ready=ready, sleep_seconds=sleep_seconds)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "RUNTIME_DIRECTORY": str(runtime_dir),
        "COMPOSE_FILE": str(_REPO_ROOT / "deploy" / "compose.yaml"),
        "QA_DOCKER_LOG": str(tmp_path / "docker.log"),
    }
    return subprocess.Popen(
        ["/bin/sh", str(_RESTORE_VERIFY_SCRIPT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_staged_password(tmp_path: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if list((tmp_path / "runtime").iterdir()):
            return
        assert proc.poll() is None, "restore-verify.sh exited before staging its password file"
        time.sleep(0.05)
    raise AssertionError("restore-verify.sh never staged its scratch password file")


def test_restore_verify_removes_the_scratch_password_on_a_readiness_timeout(
    tmp_path: Path,
) -> None:
    """The round-2 cleanup fix, executed rather than grepped: with the
    scratch instance never becoming ready, the script must still leave no
    plaintext password file behind in its staging directory.

    (`tests/unit/test_deploy_topology_regression.py`'s companion test for
    this fix only regex-checks the *ordering* of `rm -f` against the
    readiness loop in the script's source text, which cannot observe
    whether cleanup runs at all.)"""
    proc = _start_restore_verify(tmp_path, ready=False)
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()
        raise
    assert proc.returncode != 0
    leftovers = list((tmp_path / "runtime").iterdir())
    assert not leftovers, f"scratch password file survived a readiness timeout: {leftovers}"


def test_restore_verify_completes_and_leaves_no_scratch_password_behind(
    tmp_path: Path,
) -> None:
    """The whole script, executed end to end against stand-in `docker`
    binaries: it must reach the restore and verify steps, remove the
    scratch container, and leave nothing in its staging directory.

    Nothing previously executed `restore-verify.sh` past its
    `$RUNTIME_DIRECTORY` guard — every other test of it is a regex over its
    source text — so a syntax error or a reordering anywhere below line 53
    would not have been caught by any test in this repository."""
    log = tmp_path / "docker.log"
    proc = _start_restore_verify(tmp_path, ready=True)
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()
        raise
    calls = log.read_text().splitlines()
    assert proc.returncode == 0, f"restore drill failed on the happy path: {calls}"
    assert not list((tmp_path / "runtime").iterdir()), "scratch password file survived the drill"
    assert any("backup restore" in line for line in calls), calls
    assert any("backup verify" in line for line in calls), calls
    assert any(line.startswith("docker rm -f finance-restore-drill-") for line in calls), (
        f"the throwaway scratch container was never removed: {calls}"
    )


def test_restore_verify_cleans_its_staging_directory_on_sigterm(tmp_path: Path) -> None:
    """The half of the `INT`/`TERM` path that does work: the plaintext
    scratch password is gone once the signal has been handled."""
    proc = _start_restore_verify(tmp_path, ready=False, sleep_seconds="0.4")
    try:
        _wait_for_staged_password(tmp_path, proc)
        proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and list((tmp_path / "runtime").iterdir()):
            time.sleep(0.1)
        assert not list((tmp_path / "runtime").iterdir()), "scratch password file survived SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def test_restore_verify_stops_working_after_sigterm(tmp_path: Path) -> None:
    """`systemctl stop finance-restore-drill` (or `TimeoutStartSec=900`
    expiring) sends `SIGTERM`. The handler is `trap cleanup EXIT INT TERM`,
    not `trap 'cleanup; exit 143' TERM`, so once cleanup returns the shell
    resumes the readiness loop it was interrupted in and runs to its own
    conclusion — up to ~60s later with the real `sleep 2` — before systemd
    escalates to `SIGKILL`.

    Not merely a slow stop. Cleanup has already run `docker rm -f
    "$SCRATCH_CONTAINER"` and deleted the password file, so every step the
    script takes afterwards operates on resources it just destroyed: here,
    more `docker exec ... pg_isready` probes against a container cleanup
    removed; on the manual (`mktemp -d`) staging path, a re-creation of the
    plaintext password file inside a directory cleanup already `rm -rf`'d;
    and if the signal lands after readiness, a restore + verify run against
    a scratch database that no longer exists — whose failure is then
    recorded in `ops.backup_runs` as the drill's result.

    The status systemd/journald sees is the script's own exit 1 ("scratch
    instance never became ready"), not termination by signal, so the weekly
    drill's failure is misattributed during exactly the incident where the
    drill matters.

    The handler must exit: `trap 'cleanup; exit 143' TERM`, `exit 130` for
    `INT`.
    """
    log = tmp_path / "docker.log"
    proc = _start_restore_verify(tmp_path, ready=False, sleep_seconds="0.4")
    try:
        _wait_for_staged_password(tmp_path, proc)
        proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        alive = proc.poll() is None
        lines = log.read_text().splitlines() if log.exists() else []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    after_cleanup = []
    for index, line in enumerate(lines):
        if line.startswith("docker rm -f"):
            after_cleanup = lines[index + 1 :]
            break
    assert not alive, "restore-verify.sh was still running 8s after SIGTERM"
    assert not after_cleanup, (
        f"restore-verify.sh kept driving docker after its own cleanup ran: {after_cleanup[:3]}"
    )


def test_credential_file_names_cover_every_variable_the_wrapper_maps() -> None:
    """`_CREDENTIAL_FILES` in the round-3 test file is hand-maintained to
    mirror `with-production-env.sh`'s `_credential_names`. If the script
    grows a credential the table does not know about, every drift test
    above silently stops covering it — including the reverse-drift test
    added here."""
    wrapper = _WRAPPER.read_text()
    block = re.search(r"_credential_names\(\)\s*\{(.*?)\n\}", wrapper, flags=re.DOTALL)
    assert block, "could not locate _credential_names() in with-production-env.sh"
    declared = set(re.findall(r'"[a-z0-9_]+=([A-Z0-9_]+)"', block.group(1)))
    assert declared == set(_CREDENTIAL_FILES), (
        "with-production-env.sh's credential table and the test fixture's "
        f"_CREDENTIAL_FILES have drifted: only in script={declared - set(_CREDENTIAL_FILES)}, "
        f"only in tests={set(_CREDENTIAL_FILES) - declared}"
    )
