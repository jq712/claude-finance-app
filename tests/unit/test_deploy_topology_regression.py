"""Adversarial regression tests for `deploy/scripts/with-production-env.sh`
and the restore-drill unit (QA round 2 + round 3, Milestone 7 —
handoff §19/§20, ADR-007, ADR-008, ADR-016, ADR-019).

Round 1 (`tests/unit/test_ops_host_env_regression.py`, formerly
`test_ops_compose_env_regression.py`) proved that the runner-invocation
wrapper merges rather than replaces the environment. Round 2 found three
defects in the pre-ADR-016 topology (all fixed by ADR-016's D1/D2/D7) and
one probe design flaw (QA-26, fixed by D3's `probe_release` — its own
tests moved to `test_deploy_topology_baremetal.py` under ADR-019, since
they no longer touch anything in this file's remaining subject matter).
Round 3 found that ADR-016's own D1 implementation (`deploy/scripts/
with-production-env.sh`) introduced two new defects of its own — a
plaintext credential temp file that survived `exec` (never cleaned up) and
a credential-content round-trip that could inject one job's variable into
another's or silently truncate a multi-line value.

ADR-019 removed `deploy/compose.yaml`/`deploy/compose.dev.yaml`/`Dockerfile`
and, with them, this file's former compose.yaml-vs-wrapper drift test (it
asserted the wrapper's job matrix against literal compose.yaml service
blocks, which no longer exist to compare against) — `with-production-env.sh`
itself was deliberately left in scope for a later backup/restore-adaptation
follow-up (ADR-015's "Revisit when"), so its own behavior stays covered
here. What remains is direct regression coverage for every wrapper/
restore-drill defect found so far, executing the real scripts under
`/bin/sh` against synthetic fixtures — no Docker, no systemd, no real
credentials, but real shell semantics instead of a regex guess at them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO_ROOT / "deploy" / "scripts" / "with-production-env.sh"
_RESTORE_DRILL_UNIT = _REPO_ROOT / "deploy" / "systemd" / "finance-restore-drill.service"
_RESTORE_VERIFY_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "restore-verify.sh"

# stem in $CREDENTIALS_DIRECTORY -> the env var with-production-env.sh
# exports, mirroring the script's own `_credential_names`.
_CREDENTIAL_FILES = {
    "PLAID_CLIENT_ID": "plaid_client_id",
    "PLAID_SECRET": "plaid_secret",
    "PLAID_ACCESS_TOKEN": "plaid_access_token",
    "PLAID_WEBHOOK_SECRET": "plaid_webhook_secret",
    "OPENAI_API_KEY": "openai_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "FINANCE_MIGRATOR_DB_PASSWORD": "finance_migrator_db_password",
    "FINANCE_APP_DB_PASSWORD": "finance_app_db_password",
    "FINANCE_AGENT_DB_PASSWORD": "finance_agent_db_password",
    "FINANCE_OBSERVER_DB_PASSWORD": "finance_observer_db_password",
    "FINANCE_BACKUP_DB_PASSWORD": "finance_backup_db_password",
    "BACKUP_ENCRYPTION_KEY": "backup_encryption_key",
}


def _wrapper_exports(job: str, *, agent_provider: str = "openai") -> set[str]:
    """Run the real `with-production-env.sh` for `job` against a synthetic
    `$CREDENTIALS_DIRECTORY` holding every known credential, and return
    the set of credential-shaped env vars it actually exported into the
    child process — the job's real, executed requirement, not a regex
    guess at the script's logic."""
    tmp_dir = tempfile.mkdtemp()
    try:
        for env_var, stem in _CREDENTIAL_FILES.items():
            (Path(tmp_dir) / stem).write_text(f"dummy-{env_var.lower()}")
        env = {**os.environ, "CREDENTIALS_DIRECTORY": tmp_dir, "AGENT_PROVIDER": agent_provider}
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), job, "--", "env"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        shutil.rmtree(tmp_dir)
    assert result.returncode == 0, f"wrapper failed for job {job!r}: {result.stderr}"
    exported = set()
    for line in result.stdout.splitlines():
        name, _, _ = line.partition("=")
        if name in _CREDENTIAL_FILES:
            exported.add(name)
    return exported


# ---------------------------------------------------------------------------
# with-production-env.sh's own credential-scoping behavior.
# ---------------------------------------------------------------------------


def test_deploy_job_excludes_plaid_backup_and_provider_credentials() -> None:
    """The `deploy` service is the one with Docker socket access — ADR-016's
    Context argues that boundary is defense-in-depth, not a containment
    guarantee, but it's still worth keeping honest: it must never hold the
    Plaid production access token, the backup encryption key, or a model
    provider key.

    `FINANCE_AGENT_DB_PASSWORD` is included deliberately, not just for
    symmetry: `probe_release` runs `docker compose --profile app run --rm
    app finance selfcheck` *from inside this job's own process*, so
    Compose interpolates the `app` service block against this same
    environment — this pins that `deploy` deliberately leaves
    `FINANCE_AGENT_DB_PASSWORD`/the provider key unresolved there rather
    than growing this job's set to cover a service it merely invokes.
    `finance selfcheck` never touches `AGENT_DATABASE_URL` or a provider
    key (`src/finance_app/ops/selfcheck.py`), so this is safe as long as
    that stays true."""
    exported = _wrapper_exports("deploy")
    forbidden = {
        "PLAID_CLIENT_ID",
        "PLAID_SECRET",
        "PLAID_ACCESS_TOKEN",
        "PLAID_WEBHOOK_SECRET",
        "BACKUP_ENCRYPTION_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FINANCE_AGENT_DB_PASSWORD",
    }
    leaked = exported & forbidden
    assert not leaked, f"job 'deploy' exported {leaked}, which it must never hold"


# ---------------------------------------------------------------------------
# with-production-env.sh behavior (QA round 3).
# ---------------------------------------------------------------------------


def test_wrapper_leaves_no_credential_file_behind_after_a_successful_run() -> None:
    """Round 3: the previous implementation staged credentials in a
    `mktemp` file and relied on an `EXIT` trap to remove it — but `exec
    "$@"` at the end replaces the process image, so the trap never ran,
    and the plaintext file survived on disk indefinitely (worse for
    `finance-app.service`'s `RemainAfterExit=yes`)."""
    tmp_dir = tempfile.mkdtemp()
    scratch_tmpdir = tempfile.mkdtemp()
    try:
        (Path(tmp_dir) / "finance_app_db_password").write_text("apppw")
        before = set(os.listdir(scratch_tmpdir))
        env = {**os.environ, "CREDENTIALS_DIRECTORY": tmp_dir, "TMPDIR": scratch_tmpdir}
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "health", "--", "/bin/true"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        after = set(os.listdir(scratch_tmpdir))
        assert after == before, f"wrapper left files behind in $TMPDIR: {after - before}"
    finally:
        shutil.rmtree(tmp_dir)
        shutil.rmtree(scratch_tmpdir)


def test_wrapper_rejects_a_credential_containing_an_embedded_newline() -> None:
    """A multi-line credential must never be silently truncated (the
    previous temp-file round-trip re-parsed credential *content* as
    `NAME=VALUE` shell assignments, so a newline in `BACKUP_ENCRYPTION_KEY`
    both truncated the real value and could inject an arbitrary variable
    into the job's environment) — reject it outright instead."""
    tmp_dir = tempfile.mkdtemp()
    try:
        (Path(tmp_dir) / "finance_app_db_password").write_text("apppw")
        (Path(tmp_dir) / "finance_backup_db_password").write_text("bkpw")
        (Path(tmp_dir) / "backup_encryption_key").write_text(
            "line1\nBACKUP_ENCRYPTION_KEY=attacker-supplied"
        )
        env = {**os.environ, "CREDENTIALS_DIRECTORY": tmp_dir}
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "backup", "--", "/bin/true"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "attacker-supplied" not in result.stdout
        assert "embedded newline" in result.stderr
    finally:
        shutil.rmtree(tmp_dir)


def test_wrapper_rejects_a_credential_containing_a_carriage_return() -> None:
    """CRLF line endings (a Windows-side credential source, an editor, a
    paste path) aren't caught by the embedded-newline check — `wc -l`
    still sees one line — but silently corrupt the value just the same:
    a `BACKUP_ENCRYPTION_KEY` ending in `\\r` would encrypt backups under
    a passphrase that differs from whatever was recorded out-of-band,
    discovered only at restore time."""
    tmp_dir = tempfile.mkdtemp()
    try:
        (Path(tmp_dir) / "finance_app_db_password").write_text("app\rpw")
        env = {**os.environ, "CREDENTIALS_DIRECTORY": tmp_dir}
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "health", "--", "/bin/true"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "carriage return" in result.stderr
    finally:
        shutil.rmtree(tmp_dir)


def test_wrapper_rejects_a_whitespace_only_credential() -> None:
    tmp_dir = tempfile.mkdtemp()
    try:
        (Path(tmp_dir) / "finance_app_db_password").write_text("apppw")
        (Path(tmp_dir) / "finance_backup_db_password").write_text("bkpw")
        (Path(tmp_dir) / "backup_encryption_key").write_text("   ")
        env = {**os.environ, "CREDENTIALS_DIRECTORY": tmp_dir}
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "backup", "--", "/bin/true"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "BACKUP_ENCRYPTION_KEY" in result.stderr
    finally:
        shutil.rmtree(tmp_dir)


def test_wrapper_does_not_trust_an_ambient_value_for_a_missing_credential() -> None:
    """A required credential must come from `$CREDENTIALS_DIRECTORY`, never
    from whatever this process happened to inherit (an `EnvironmentFile=`,
    a stray export in an interactive shell) — the file being absent must
    fail exactly as hard as it failing to decrypt would."""
    tmp_dir = tempfile.mkdtemp()
    try:
        (Path(tmp_dir) / "finance_app_db_password").write_text("apppw")
        # finance_backup_db_password and backup_encryption_key deliberately
        # absent from $CREDENTIALS_DIRECTORY.
        env = {
            **os.environ,
            "CREDENTIALS_DIRECTORY": tmp_dir,
            "FINANCE_BACKUP_DB_PASSWORD": "ambient-value-must-not-be-trusted",
            "BACKUP_ENCRYPTION_KEY": "ambient-value-must-not-be-trusted",
        }
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "backup", "--", "/bin/true"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, (
            "wrapper accepted a credential from the ambient environment instead of "
            "requiring it to come from $CREDENTIALS_DIRECTORY"
        )
    finally:
        shutil.rmtree(tmp_dir)


def test_wrapper_does_not_leak_a_non_required_credential_into_the_job() -> None:
    tmp_dir = tempfile.mkdtemp()
    try:
        (Path(tmp_dir) / "finance_app_db_password").write_text("apppw")
        (Path(tmp_dir) / "finance_backup_db_password").write_text("bkpw")
        (Path(tmp_dir) / "backup_encryption_key").write_text("key")
        env = {
            **os.environ,
            "CREDENTIALS_DIRECTORY": tmp_dir,
            "PLAID_SECRET": "ambient-plaid-secret",
        }
        result = subprocess.run(
            ["/bin/sh", str(_WRAPPER), "backup", "--", "/bin/sh", "-c", "echo [$PLAID_SECRET]"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[]", (
            f"job 'backup' must never see PLAID_SECRET, even one already in this "
            f"process's own environment; got {result.stdout!r}"
        )
    finally:
        shutil.rmtree(tmp_dir)


def test_wrapper_rejects_an_unknown_job() -> None:
    result = subprocess.run(
        ["/bin/sh", str(_WRAPPER), "not-a-real-job", "--", "/bin/true"],
        env={**os.environ, "CREDENTIALS_DIRECTORY": tempfile.mkdtemp()},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "unknown job" in result.stderr


# ---------------------------------------------------------------------------
# D7 — restore-drill staging directory.
# ---------------------------------------------------------------------------


def test_restore_drill_unit_declares_a_runtime_directory() -> None:
    """ADR-016 D7: `restore-verify.sh`'s scratch password must be staged
    somewhere the host Docker daemon can resolve as a bind-mount source —
    `RuntimeDirectory=` (`/run/<name>`, host mount namespace), never a
    `PrivateTmp=true` unit's private `/tmp` (QA-25: the daemon runs outside
    that namespace and silently substitutes an empty directory)."""
    unit = _RESTORE_DRILL_UNIT.read_text()
    assert re.search(r"^RuntimeDirectory=\S+", unit, flags=re.MULTILINE), (
        "finance-restore-drill.service does not declare RuntimeDirectory="
    )
    assert re.search(r"^RuntimeDirectoryMode=0700", unit, flags=re.MULTILINE), (
        "finance-restore-drill.service's RuntimeDirectory= must be 0700 — it stages "
        "a plaintext scratch password"
    )


def test_restore_verify_refuses_to_fall_back_to_tmp_under_systemd() -> None:
    """If `$CREDENTIALS_DIRECTORY` is set (i.e. running under the systemd
    unit) but `$RUNTIME_DIRECTORY` is not, the script must refuse rather
    than silently falling back to a bare `mktemp -d` under `/tmp` — that
    fallback is exactly what reproduced QA-25 under `PrivateTmp=true`.

    Actually executes the script up to that check (it runs before any
    Docker/network access, so no daemon is needed) rather than only
    grepping for the guard's source text — a grep-only version of this
    test would stay green even if the `if` condition itself were wrong or
    silently short-circuited."""
    result = subprocess.run(
        ["/bin/sh", str(_RESTORE_VERIFY_SCRIPT)],
        env={**os.environ, "CREDENTIALS_DIRECTORY": "/does/not/matter"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Refusing to fall back to /tmp" in result.stderr

    assert not re.search(r'-v\s+"\$SCRATCH_PASSWORD_FILE:', _RESTORE_VERIFY_SCRIPT.read_text()), (
        "restore-verify.sh bind-mounts a single mktemp *file* again — D7 requires "
        "bind-mounting the *directory* the Docker daemon can actually resolve"
    )


def test_restore_verify_removes_the_scratch_password_only_after_readiness() -> None:
    """Round 3: the scratch password file used to be removed immediately
    after `docker run -d`, before the scratch Postgres was confirmed
    ready. Since D7 bind-mounts the *directory* (not the file), that
    deletion is visible inside the container the instant it happens —
    racing the container's own startup and reproducing the same "scratch
    instance never became ready" failure D7 was meant to fix. The removal
    must come after the `pg_isready` loop, not before `docker run`."""
    script = _RESTORE_VERIFY_SCRIPT.read_text()
    run_match = re.search(r"docker run -d.*?\n(?:.*\\\n)*.*\n", script)
    ready_match = re.search(r"until docker exec .* pg_isready", script)
    rm_match = re.search(r'rm -f "\$SCRATCH_PASSWORD_FILE"', script)
    assert run_match and ready_match and rm_match, "expected script structure not found"
    assert rm_match.start() > ready_match.start() > run_match.start(), (
        "the scratch password file must be removed only after the readiness loop, "
        "not immediately after `docker run -d`"
    )
