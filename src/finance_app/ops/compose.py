"""Thin wrapper around `docker compose` for the production stack (handoff
§20, ADR-007, ADR-008).

Used only by `finops restart`/`deploy`/`rollback` (`cli/finops.py`), which
are meant to run **on the VPS**, operating on the compose stack already
running there — never invoked by CI or the Claude Code engineering
environment over SSH (ADR-007 forbids exactly that). `finops` shelling out
to `docker compose` locally, on the host it's already running on, is the
narrow supervised interface ADR-007 describes; it is not the same thing as
an engineering agent reaching into production over SSH.

`runner` is injectable so these commands are unit-testable without a real
Docker daemon — tests substitute a fake that records the argv it was
called with instead of executing anything.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_COMPOSE_FILE = "deploy/compose.yaml"


class ComposeError(RuntimeError):
    """A `docker compose` invocation failed. Message is built only from
    argv and stderr — both already argv-safe, no secrets pass through
    compose invocations (those come from systemd credentials at container
    start, not CLI arguments).

    `stdout` carries whatever the process wrote before it exited non-zero,
    if any — `ops.status.probe_release` needs this: a `finance selfcheck`
    run that reports itself unhealthy still exits 1 (`ops.compose.run_compose`
    treats it as `ComposeError` since `check=True`), but its JSON payload on
    stdout is real diagnostic data, not a docker/compose failure, and must
    not be discarded."""

    def __init__(self, message: str, *, stdout: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout


def compose_command(compose_file: str, *args: str) -> list[str]:
    return ["docker", "compose", "-f", compose_file, *args]


def run_compose(
    compose_file: str,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """`env` is *merged onto* the current process environment, never
    substituted for it (QA-1 — the previous behavior passed `env` straight
    to `subprocess.run`, which replaces the child's entire environment,
    dropping `PATH`/`HOME`/every `${VAR:?required}` `deploy/compose.yaml`
    needs to interpolate). Callers pass only the deploy-specific overlay
    (e.g. `{"RELEASE_ID": release_id}`); the credentials
    `deploy/scripts/with-production-env.sh` exported into this process's
    environment survive into the child unchanged.

    `timeout` (ADR-016 D3) bounds the call so a hung `docker compose` —
    e.g. `probe_release`'s one-shot selfcheck run against a release that
    never comes up — cannot hang `finops deploy` indefinitely; a
    `subprocess.TimeoutExpired` surfaces as `ComposeError` like any other
    failure."""
    command = compose_command(compose_file, *args)
    merged_env = {**os.environ, **(env or {})}
    try:
        return runner(
            command, env=merged_env, text=True, capture_output=True, check=True, timeout=timeout
        )
    except subprocess.CalledProcessError as exc:
        raise ComposeError(
            f"{' '.join(command)} failed (exit {exc.returncode}): {exc.stderr[:500]}",
            stdout=exc.stdout or "",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ComposeError(f"{' '.join(command)} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ComposeError(
            "docker CLI not found — finops deploy/rollback/restart must run on a host "
            "with Docker installed (the VPS), not inside the engineering environment."
        ) from exc
