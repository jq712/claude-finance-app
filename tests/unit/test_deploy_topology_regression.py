"""Adversarial regression tests for the *deployed* topology
(QA round 2 + round 3, Milestone 7 — handoff §19/§20, ADR-007, ADR-008,
ADR-016).

Round 1 (`tests/unit/test_ops_compose_env_regression.py`) proved that
`run_compose` merges rather than replaces the environment. Round 2 found
three defects in the pre-ADR-016 topology (all fixed by ADR-016's D1/D2/D7)
and one probe design flaw (QA-26, fixed by D3's `probe_release`). Round 3
found that ADR-016's own D1 implementation (`deploy/scripts/
with-production-env.sh`) introduced two new defects of its own — a
plaintext credential temp file that survived `exec` (never cleaned up) and
a credential-content round-trip that could inject one job's variable into
another's or silently truncate a multi-line value — and that the drift
test this file promises (compose.yaml's and with-production-env.sh's own
comments both point here) had never actually been written, so none of this
was mechanically enforced.

This file is now that drift test, plus direct regression coverage for
every defect found so far. Static/config-shape assertions run with no
Docker daemon required (CI's `unit-tests` job); the drift test and the
wrapper-behavior tests below execute the real `with-production-env.sh`
under `/bin/sh` against a synthetic `$CREDENTIALS_DIRECTORY` — still no
Docker, no systemd, no real credentials, but real shell semantics instead
of a regex guess at them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from finance_app.ops.compose import ComposeError
from finance_app.ops.status import probe_release

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "compose.yaml"
_WRAPPER = _REPO_ROOT / "deploy" / "scripts" / "with-production-env.sh"
_RESTORE_DRILL_UNIT = _REPO_ROOT / "deploy" / "systemd" / "finance-restore-drill.service"
_RESTORE_VERIFY_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "restore-verify.sh"

# `${NAME:?message}` — a variable Compose refuses to interpolate without.
_REQUIRED_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+):\?")
_CREDENTIAL_VAR_REF_RE = re.compile(r"\$\{([A-Z0-9_]+):-")

# stem in $CREDENTIALS_DIRECTORY -> the env var deploy/compose.yaml expects,
# mirroring with-production-env.sh's own `_credential_names`.
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

# job -> compose service it maps to 1:1 for credential purposes. `app` is
# handled separately (its provider key is either/or, not fixed); `health`
# and `restore-drill` are deliberately excluded — `health` runs the `app`
# service but is a documented, narrower subset of its credential needs
# (ops/health.py never touches AGENT_DATABASE_URL or a provider key, so
# those interpolating empty is harmless), and `restore-drill` has no
# compose service of its own (a bare `docker run`, not `docker compose
# run` — see restore-verify.sh).
_JOB_TO_SERVICE = {
    "postgres": "postgres",
    "migrate": "migrate",
    "sync": "sync",
    "backup": "backup",
    "finops": "finops",
    "deploy": "deploy",
}

# Known, documented, accepted gaps between a service's declared `${VAR:-}`
# references and what its job actually loads — not defects.
_KNOWN_GAPS: dict[str, set[str]] = {
    # Milestone 8: the webhook secret isn't minted yet and nothing reads
    # it in `sync` until the webhook endpoint exists (ADR-016 Revisit-when,
    # security review finding 9). Must gain real enforcement before
    # Milestone 8 treats an empty secret as "verification configured".
    "sync": {"PLAID_WEBHOOK_SECRET"},
}


def _service_blocks(compose_text: str) -> dict[str, str]:
    """Split the `services:` mapping into `{name: raw text}`. A plain text
    split rather than a YAML parse: PyYAML is not a dependency of this
    project and adding one just to assert on a config file would be a new
    dependency for a test, which CLAUDE.md's "what problem does it solve
    now" rule does not justify."""
    lines = compose_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "services:")
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():  # left the `services:` mapping
            break
        match = re.match(r"^  ([a-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


def _credential_vars_referenced(block: str) -> set[str]:
    return {name for name in _CREDENTIAL_VAR_REF_RE.findall(block) if name in _CREDENTIAL_FILES}


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
# The drift test: with-production-env.sh's job matrix vs. compose.yaml.
# ---------------------------------------------------------------------------


def test_no_compose_service_declares_a_required_interpolation_variable() -> None:
    """ADR-016 D1: Compose interpolates every `${VAR}` in the whole file
    before running any one service, so a whole-file `${VAR:?required}`
    could never express a per-job requirement (QA-20) — enforcement moved
    to `with-production-env.sh`'s job matrix, tested below.

    Scans only non-comment lines: the file's own header comment uses the
    literal string `${VAR:?required}` as an illustrative example of the
    pattern that must no longer appear for real — a naive whole-file
    regex match against that comment is exactly how the original version
    of this test (QA-33/security review) stayed green whether or not the
    real defect was fixed."""
    non_comment_lines = "\n".join(
        line
        for line in _COMPOSE_FILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    required = set(_REQUIRED_VAR_RE.findall(non_comment_lines))
    assert not required, (
        f"deploy/compose.yaml still has ${{VAR:?required}} interpolation for {required}; "
        "ADR-016 D1 requires every credential-shaped variable to use ${VAR:-} instead, "
        "with requirement enforcement living in with-production-env.sh's job matrix"
    )


@pytest.mark.parametrize("job,service", sorted(_JOB_TO_SERVICE.items()))
def test_wrapper_job_covers_every_credential_its_compose_service_references(
    job: str, service: str
) -> None:
    compose_text = _COMPOSE_FILE.read_text()
    block = _service_blocks(compose_text)[service]
    referenced = _credential_vars_referenced(block) - _KNOWN_GAPS.get(service, set())
    exported = _wrapper_exports(job)
    missing = referenced - exported
    assert not missing, (
        f"with-production-env.sh job {job!r} does not export {missing}, which "
        f"deploy/compose.yaml's {service!r} service references via ${{VAR:-}} — that "
        "service would start with those credentials silently empty"
    )


def test_app_job_covers_its_credentials_and_only_the_active_provider_key() -> None:
    """`app`'s provider key is either/or (AGENT_PROVIDER), not a fixed
    entry in the job matrix — both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`
    appear in compose.yaml's `app` block unconditionally (the inactive
    one interpolates to an empty string, which is fine — `app` never uses
    it), so this is checked separately from the generic per-service loop
    above rather than requiring both providers' keys at once."""
    compose_text = _COMPOSE_FILE.read_text()
    block = _service_blocks(compose_text)["app"]
    referenced = _credential_vars_referenced(block) - {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}

    exported_openai = _wrapper_exports("app", agent_provider="openai")
    assert referenced <= exported_openai
    assert "OPENAI_API_KEY" in exported_openai
    assert "ANTHROPIC_API_KEY" not in exported_openai, (
        "job 'app' must export only the *active* provider's key, never both"
    )

    exported_anthropic = _wrapper_exports("app", agent_provider="anthropic")
    assert referenced <= exported_anthropic
    assert "ANTHROPIC_API_KEY" in exported_anthropic
    assert "OPENAI_API_KEY" not in exported_anthropic


def test_deploy_job_excludes_plaid_backup_and_provider_credentials() -> None:
    """The `deploy` service is the one with Docker socket access — ADR-016's
    Context argues that boundary is defense-in-depth, not a containment
    guarantee, but it's still worth keeping honest: it must never hold the
    Plaid production access token, the backup encryption key, or a model
    provider key."""
    exported = _wrapper_exports("deploy")
    forbidden = {
        "PLAID_CLIENT_ID",
        "PLAID_SECRET",
        "PLAID_ACCESS_TOKEN",
        "PLAID_WEBHOOK_SECRET",
        "BACKUP_ENCRYPTION_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
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
# D2 — app has no restart policy and no baked-in command.
# ---------------------------------------------------------------------------


def test_app_service_has_no_restart_policy_or_command() -> None:
    """ADR-016 D2: a container kept alive with a placeholder one-shot
    command (the prior `["finance", "status"]` + `restart: unless-stopped`)
    crash-loops forever the instant that command exits (QA-24). `app` must
    have neither a `restart:` policy nor a baked-in `command:` — it runs
    only as `docker compose --profile app run --rm app <cmd>`."""
    compose_text = _COMPOSE_FILE.read_text()
    app_block = _service_blocks(compose_text)["app"]
    assert not re.search(r"^    restart:", app_block, flags=re.MULTILINE), (
        "app has a restart: policy — combined with no persistent process to keep "
        "alive (D2), this crash-loops"
    )
    assert not re.search(r"^    command:", app_block, flags=re.MULTILINE), (
        "app has a baked-in command: — it must be started only via "
        "`docker compose --profile app run --rm app <cmd>`"
    )


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
    fallback is exactly what reproduced QA-25 under `PrivateTmp=true`."""
    script = _RESTORE_VERIFY_SCRIPT.read_text()
    assert "RUNTIME_DIRECTORY" in script, (
        "restore-verify.sh no longer references $RUNTIME_DIRECTORY at all"
    )
    assert not re.search(r'-v\s+"\$SCRATCH_PASSWORD_FILE:', script), (
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


# ---------------------------------------------------------------------------
# D3 — probe_release replaces the exec-based, timing-dependent probe.
# ---------------------------------------------------------------------------


def test_probe_release_reports_healthy_when_the_right_release_selfchecks_clean() -> None:
    payload = json.dumps({"release_id": "abc1234", "overall": "healthy"})

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0, payload + "\n", "")

    result = probe_release(release_id="abc1234", run_compose_fn=fake_runner)
    assert result["status"] == "healthy"
    assert result["reported_release_id"] == "abc1234"


def test_probe_release_detects_a_wrong_image() -> None:
    """QA-26's underlying concern — a probe that could report healthy for
    the wrong release — is structurally eliminated by D3: `probe_release`
    runs the exact image under deployment and checks what it reports about
    itself, so a stale/wrong image is caught by content, not by luck."""
    payload = json.dumps({"release_id": "stale999", "overall": "healthy"})

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return subprocess.CompletedProcess([], 0, payload + "\n", "")

    result = probe_release(release_id="abc1234", run_compose_fn=fake_runner)
    assert result["status"] == "wrong_image"
    assert result["reported_release_id"] == "stale999"


def test_probe_release_reports_unhealthy_on_a_nonzero_selfcheck_exit() -> None:
    payload = json.dumps({"release_id": "abc1234", "overall": "unhealthy"})

    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ComposeError("docker compose run ... failed (exit 1): ...", stdout=payload + "\n")

    result = probe_release(release_id="abc1234", run_compose_fn=fake_runner)
    assert result["status"] == "unhealthy"
    assert result["reported_release_id"] == "abc1234"


def test_probe_release_reports_unreachable_when_docker_itself_fails() -> None:
    def fake_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ComposeError("docker CLI not found")

    result = probe_release(release_id="abc1234", run_compose_fn=fake_runner)
    assert result["status"] == "unreachable"
    assert result["reported_release_id"] is None
