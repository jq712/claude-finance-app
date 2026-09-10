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

import subprocess
from collections.abc import Callable

Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_COMPOSE_FILE = "deploy/compose.yaml"


class ComposeError(RuntimeError):
    """A `docker compose` invocation failed. Message is built only from
    argv and stderr — both already argv-safe, no secrets pass through
    compose invocations (those come from systemd credentials at container
    start, not CLI arguments)."""


def compose_command(compose_file: str, *args: str) -> list[str]:
    return ["docker", "compose", "-f", compose_file, *args]


def run_compose(
    compose_file: str,
    *args: str,
    env: dict[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    command = compose_command(compose_file, *args)
    try:
        return runner(command, env=env, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise ComposeError(
            f"{' '.join(command)} failed (exit {exc.returncode}): {exc.stderr[:500]}"
        ) from exc
    except FileNotFoundError as exc:
        raise ComposeError(
            "docker CLI not found — finops deploy/rollback/restart must run on a host "
            "with Docker installed (the VPS), not inside the engineering environment."
        ) from exc
