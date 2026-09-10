"""Adversarial regression tests for the *deployed* topology
(QA round 2, Milestone 7 — handoff §19/§20, ADR-007, ADR-008).

Round 1 (`tests/unit/test_ops_compose_env_regression.py`) proved that
`run_compose` merges rather than replaces the environment. That fix is
correct in isolation but was verified against the *pytest* process's
environment, which `tests/conftest.py` pre-seeds with every
`FINANCE_*_DB_PASSWORD`/`PLAID_*` value via `os.environ.setdefault`. The
environment that actually matters is the one inside the `deploy` Compose
service, which is where `deploy/scripts/finops.sh` and
`docs/runbooks/deploy.md` say `finops deploy`/`rollback`/`restart` run —
and that environment is built solely from the `deploy` service's own
`environment:` block in `deploy/compose.yaml`.

Every test here encodes a defect confirmed against real Docker / real
`docker compose` on this branch. Marked `xfail(strict=True)` so the suite
stays green until the implementation is fixed and then fails loudly
(XPASS) the moment it is — delete the marker then, not the test.

These are deliberately static/config-shape assertions (no Docker daemon
required) so they run in CI's `unit-tests` job, which is the gate that
should have caught all three before this branch reached a VPS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "compose.yaml"
_RESTORE_DRILL_UNIT = _REPO_ROOT / "deploy" / "systemd" / "finance-restore-drill.service"
_RESTORE_VERIFY_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "restore-verify.sh"

# `${NAME:?message}` — a variable Compose refuses to interpolate without.
_REQUIRED_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+):\?")


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-20: the `deploy` service's environment omits every compose-interpolation "
        "variable, so `finops deploy` cannot run `docker compose` at all."
    ),
)
def test_deploy_service_environment_covers_every_required_compose_variable() -> None:
    """`finops deploy`/`rollback`/`restart` run *inside* the `deploy`
    Compose service and shell out to `docker compose -f
    deploy/compose.yaml ...` from there. Compose interpolates every
    `${VAR:?...}` in the whole file before running any service — a fact
    `deploy/compose.yaml`'s own header comment states explicitly — so the
    `deploy` container's environment must carry all of them.

    It carries none. Reproduced end to end against real Docker, running
    exactly what `deploy/scripts/finops.sh` runs::

        docker compose -f deploy/compose.yaml --profile deploy \\
            run --rm -T deploy finops deploy 1234567

        deploy failed to start: docker compose -f deploy/compose.yaml pull app
        failed (exit 1): error while interpolating
        services.app.environment.AGENT_DATABASE_URL: required variable
        FINANCE_AGENT_DB_PASSWORD is missing a value
        ...
        (exit 1)

    `run_compose`'s `{**os.environ, **env}` merge (QA-1) is correct; the
    environment it merges into is empty of these values, because
    `deploy/scripts/with-production-env.sh` exports them into the *host*
    process that invokes `docker compose run`, and Compose passes only a
    service's declared `environment:` keys into the container.
    """
    compose_text = _COMPOSE_FILE.read_text()
    required = set(_REQUIRED_VAR_RE.findall(compose_text))
    assert required, "expected deploy/compose.yaml to declare ${VAR:?required} variables"

    deploy_block = _service_blocks(compose_text)["deploy"]
    declared = set(re.findall(r"^      ([A-Z0-9_]+):", deploy_block, flags=re.MULTILINE))

    missing = sorted(required - declared)
    assert not missing, (
        "the `deploy` service cannot interpolate deploy/compose.yaml without "
        f"{missing}; `finops deploy`/`rollback`/`restart` fail at their first "
        "`docker compose` call"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-24: the long-running `app` service runs `finance status`, which exits "
        "immediately, so `restart: unless-stopped` puts it in a permanent crash loop."
    ),
)
def test_app_service_command_does_not_exit_immediately() -> None:
    """`deploy/compose.yaml`'s `app` service comments say it is "kept
    long-running" and stays up "for `docker compose exec`/interactive
    use". Its `command:` is `["finance", "status"]`, which prints three
    lines and exits 0. Combined with `restart: unless-stopped`, the
    container restarts forever. Reproduced against the real image::

        docker compose -f deploy/compose.yaml up -d app
        docker compose -f deploy/compose.yaml ps -a
        deploy-app-1 ... Restarting (0) 3 seconds ago
        docker inspect deploy-app-1 --format '{{.RestartCount}}'
        8      # after ~25 seconds

    Every restart opens a database connection and re-runs the status
    query, forever, on the production VPS.

    This test asserts the narrow, checkable property: a service with a
    restart policy must not be given a command that is known to be
    one-shot. `finance status` (and the other one-shot entrypoints) belong
    to the profiled `migrate`/`sync`/`backup`/`finops` services, which
    correctly have no restart policy.
    """
    compose_text = _COMPOSE_FILE.read_text()
    app_block = _service_blocks(compose_text)["app"]

    assert "restart: unless-stopped" in app_block, "fixture assumption: app has a restart policy"

    command_match = re.search(r"^    command: (.+)$", app_block, flags=re.MULTILINE)
    assert command_match is not None, "app service declares no command"
    command = command_match.group(1)

    one_shot = ("finance status", "finance --help", "finance version", "alembic upgrade head")
    offending = [candidate for candidate in one_shot if candidate.replace(" ", '", "') in command]
    assert not offending, (
        f"`app` has restart: unless-stopped but runs the one-shot command {command}; "
        "the container exits immediately and Docker restarts it forever"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-25: PrivateTmp=true breaks restore-verify.sh's mktemp password file, which "
        "is bind-mounted into a container by the *host* Docker daemon."
    ),
)
def test_restore_drill_unit_does_not_combine_private_tmp_with_a_host_bind_mount() -> None:
    """`deploy/systemd/finance-restore-drill.service` (added in 50fee57)
    sets `PrivateTmp=true` and asserts in a comment that this "is
    compatible with restore-verify.sh's `mktemp` scratch-password file".
    It is not.

    `PrivateTmp=true` gives the unit's processes a private `/tmp` in their
    own mount namespace. `restore-verify.sh` does::

        SCRATCH_PASSWORD_FILE="$(mktemp)"                     # /tmp/tmp.XXXX, namespaced
        docker run -d ... -v "$SCRATCH_PASSWORD_FILE:/run/secrets/scratch_password:ro" ...

    The bind-mount source is resolved by the Docker **daemon**, which runs
    outside that namespace. `/tmp/tmp.XXXX` does not exist on the host
    root, so Docker creates an empty *directory* there and mounts it.
    `POSTGRES_PASSWORD_FILE=/run/secrets/scratch_password` then points at a
    directory, the scratch Postgres never starts, and the script exits via
    its own "scratch instance never became ready" path.

    Consequence: the weekly restore drill fails every week, no
    `restore_verification` row is ever written, `finops backup-status`
    reports `unverified` forever, and `aggregate_health` therefore reports
    the whole system unhealthy — the exact "a backup that has never been
    restored is not verified" signal ADR-015 exists to protect, disabled
    by a hardening change.

    Either drop `PrivateTmp=true` from this unit, or stage the password
    file somewhere outside `/tmp` (e.g. `RuntimeDirectory=`) that the
    daemon can also see.
    """
    unit = _RESTORE_DRILL_UNIT.read_text()
    script = _RESTORE_VERIFY_SCRIPT.read_text()

    mounts_a_mktemp_path = bool(
        re.search(r"mktemp", script) and re.search(r'-v\s+"\$SCRATCH_PASSWORD_FILE:', script)
    )
    assert mounts_a_mktemp_path, "fixture assumption: restore-verify.sh bind-mounts a mktemp file"

    assert "PrivateTmp=true" not in unit, (
        "finance-restore-drill.service sets PrivateTmp=true while restore-verify.sh "
        "bind-mounts a /tmp path into a container via the host Docker daemon; the "
        "daemon cannot see the unit's private /tmp and silently substitutes an empty "
        "directory, so the scratch Postgres never starts"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-26: probe_application's `docker compose exec -T app true` reports healthy "
        "for a container that is in a restart loop, if it happens to land in an up window."
    ),
)
def test_probe_application_detects_a_crash_looping_container() -> None:
    """QA-8's fix replaced a hardcoded `"healthy"` literal with a real
    probe, but the probe is `docker compose exec -T app true` — which only
    proves that a container existed and `/bin/true` ran at that instant.
    A container Docker is restarting on a loop is up for part of every
    cycle, so the probe's verdict is a coin flip decided by timing.

    Measured against the real image and the real `deploy/compose.yaml`
    `app` service (which crash-loops — see
    `test_app_service_command_does_not_exit_immediately`):

        immediately after `up -d app`  -> healthy, healthy, healthy  (3/3)
        after backoff had grown        -> unreachable (10/10)

    `finops deploy` probes immediately after `up -d`, i.e. exactly in the
    window where the answer is wrong, so it promotes a crash-looping
    release to `current` and reports a green deploy.

    The probe must consult container *state* (`docker compose ps` /
    `docker inspect` restart count or a Compose `healthcheck:`), not just
    win one exec race. This test supplies a runner that models exactly
    that container: `exec` succeeds, `ps` says `Restarting`.
    """
    from finance_app.ops.status import probe_application

    class _Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def crash_looping_runner(compose_file, *args, env=None, runner=None):  # noqa: ANN001, ANN002, ARG001
        if "ps" in args:
            return _Completed('[{"Name":"deploy-app-1","State":"restarting","ExitCode":0}]')
        # `exec` lands inside one of the container's brief up windows.
        return _Completed("")

    verdict = probe_application("deploy/compose.yaml", run_compose_fn=crash_looping_runner)

    assert verdict == "unreachable", (
        "a container Docker is actively restarting must not be reported as healthy"
    )
