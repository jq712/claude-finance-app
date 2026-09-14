"""Bare-metal replacement for the superseded `ops/compose.py` (ADR-019).

Used only by `finops restart`/`deploy`/`rollback` (`cli/finops.py`), which
are meant to run **on the VPS**, operating on the release tree already
there — never invoked by CI or the Claude Code engineering environment
over SSH (ADR-007/ADR-010 forbid exactly that; ADR-019 restates the same
invariant for the bare-metal substrate). `finops` shelling out to a
release directory's own venv binary, or to `systemctl`, locally, on the
host it is already running on, is the narrow supervised interface those
ADRs describe.

`runner` is injectable so every command here is unit-testable without a
real production host, root, or `/opt/finance` present — tests substitute
a fake that records the argv it was called with instead of executing
anything. This is a straight port of `ops/compose.py`'s injection seam
(`Runner`, `run_compose` -> `run_release`, `ComposeError` ->
`HostCommandError`), which is what let ~30 tests in this suite run
without a Docker daemon; keeping the same shape means those tests retarget
by renaming what they patch, not by being rewritten.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_RELEASE_ROOT = "/opt/finance"
RELEASES_DIRNAME = "releases"
CURRENT_LINK_NAME = "current"
VENV_BIN = ".venv/bin"

# systemd unit names this process will ever pass to `systemctl` — rejected
# before spawning anything if a configured unit doesn't match. Not a
# shell, so this is not injection-prevention; it is a fail-closed check
# that `Settings.production_units` was configured with something
# unit-shaped rather than a typo or an unrelated string.
_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9@:._-]+\.(service|timer|socket|target)$")


class HostCommandError(RuntimeError):
    """A release-tree command or `systemctl` invocation failed. Message is
    built only from argv and stderr — both already argv-safe, no secrets
    pass through these invocations (those come from `/opt/finance/.env`
    at process start, not CLI arguments).

    `stdout` carries whatever the process wrote before it exited non-zero,
    if any — `ops.status.probe_release` needs this exactly as
    `ops/compose.py`'s `ComposeError` did: a `finance selfcheck` run that
    reports itself unhealthy still exits 1 (`run_release` treats it as
    `HostCommandError` since `check=True`), but its JSON payload on stdout
    is real diagnostic data, not a launch failure, and must not be
    discarded."""

    def __init__(self, message: str, *, stdout: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout


def release_dir(release_root: str | Path, release_id: str) -> Path:
    return Path(release_root) / RELEASES_DIRNAME / release_id


def current_link(release_root: str | Path) -> Path:
    return Path(release_root) / CURRENT_LINK_NAME


def release_command(release_path: str | Path, *args: str) -> list[str]:
    if not args:
        raise ValueError("release_command requires at least the binary name")
    binary, rest = args[0], args[1:]
    return [str(Path(release_path) / VENV_BIN / binary), *rest]


def run_release(
    release_path: str | Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Runs `<release_path>/.venv/bin/<args[0]> <args[1:]>`.

    `env` is *merged onto* the current process environment, never
    substituted for it — the same QA-1 fix `ops/compose.py` carried:
    passing `env` straight to `subprocess.run` replaces the child's entire
    environment, dropping `PATH`/`HOME` and, here, the operator's
    `FINANCE_ENV_FILE` (needed so the release resolves `/opt/finance/.env`
    rather than falling back to dev defaults). Callers pass only the
    narrow overlay a given invocation needs (e.g.
    `{"RELEASE_ID": release_id}`).

    `timeout` bounds the call so a hung release binary — e.g.
    `probe_release`'s one-shot selfcheck run against a release that never
    responds — cannot hang `finops deploy` indefinitely; a
    `subprocess.TimeoutExpired` surfaces as `HostCommandError` like any
    other failure."""
    command = release_command(release_path, *args)
    merged_env = {**os.environ, **(env or {})}
    try:
        return runner(
            command, env=merged_env, text=True, capture_output=True, check=True, timeout=timeout
        )
    except subprocess.CalledProcessError as exc:
        raise HostCommandError(
            f"{' '.join(command)} failed (exit {exc.returncode}): {exc.stderr[:500]}",
            stdout=exc.stdout or "",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HostCommandError(f"{' '.join(command)} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise HostCommandError(
            f"{command[0]} not found — the release directory has no built virtualenv. "
            "finops deploy/rollback/restart must run on the VPS as the production user, "
            "against a release directory built with `uv sync --locked --no-dev`."
        ) from exc
    except OSError as exc:
        # A `PermissionError`, a `UnicodeDecodeError` (`text=True` decodes
        # strictly, so one non-UTF-8 byte anywhere on the release binary's
        # stdout raises instead of returning), or a bare `OSError` from
        # fork/posix_spawn would otherwise escape past every
        # `except HostCommandError` in the deploy path, leaving a release
        # `pending` with no auto-rollback attempted (the same class of gap
        # `ops/compose.py`'s QA-38 closed).
        raise HostCommandError(f"{' '.join(command)} failed: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise HostCommandError(f"{' '.join(command)} produced undecodable output: {exc}") from exc


def run_systemctl(
    action: str,
    *units: str,
    prefix: Sequence[str] = ("systemctl",),
    timeout: float | None = 60.0,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Runs `<prefix> <action> <units...>` — e.g. `systemctl restart
    finance-app.service`. `prefix` lets the production Unix user configure
    e.g. `("sudo", "-n", "systemctl")` if `systemctl restart` needs
    elevated rights it doesn't hold directly (`Settings.systemctl_prefix`);
    the `-n` (non-interactive) flag is the operator's responsibility to
    include — a `sudo` that blocks on a password prompt would hang
    `finops deploy` exactly the way `ops/compose.py`'s `timeout` parameter
    existed to prevent for `docker compose`.

    Every unit name is validated against `_UNIT_NAME_RE` *before*
    spawning anything — a misconfigured `Settings.production_units` fails
    with a clear `HostCommandError`, never a shell injection concern (this
    never goes through a shell) but a fail-closed check that the value is
    actually unit-shaped."""
    if not units:
        raise HostCommandError("run_systemctl requires at least one unit name")
    for unit in units:
        if not _UNIT_NAME_RE.match(unit):
            raise HostCommandError(
                f"{unit!r} does not look like a systemd unit name "
                "(expected e.g. 'finance-app.service') — refusing to invoke systemctl."
            )
    command = [*prefix, action, *units]
    try:
        return runner(command, text=True, capture_output=True, check=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise HostCommandError(
            f"{' '.join(command)} failed (exit {exc.returncode}): {exc.stderr[:500]}",
            stdout=exc.stdout or "",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HostCommandError(f"{' '.join(command)} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise HostCommandError(f"{command[0]} not found on this host.") from exc
    except OSError as exc:
        raise HostCommandError(f"{' '.join(command)} failed: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise HostCommandError(f"{' '.join(command)} produced undecodable output: {exc}") from exc


def release_is_installed(release_root: str | Path, release_id: str) -> bool:
    """A real directory exists at `releases/<release_id>` (not a symlink —
    a symlinked "release" would mean someone pointed one release id at
    another's tree, which is exactly the kind of disagreement
    `ops/identity.py` exists to catch downstream, but there is no reason
    to accept it as "installed" here) and has a built virtualenv."""
    path = release_dir(release_root, release_id)
    if not path.is_dir() or path.is_symlink():
        return False
    return (path / VENV_BIN / "finance").is_file()


def read_current_target(release_root: str | Path) -> str | None:
    """The release id `current` resolves to right now, or `None` if
    `current` does not exist, is not a symlink, or (a dangling link) its
    target does not exist. Resolves through the symlink rather than just
    reading its literal text, so a relative link
    (`current -> releases/<sha>`) and an absolute one report the same
    thing."""
    link = current_link(release_root)
    if not link.is_symlink():
        return None
    target = link.resolve()
    if not target.is_dir():
        return None
    return target.name


def repoint_current(release_root: str | Path, release_id: str) -> str | None:
    """Atomically repoints `current` at `releases/<release_id>`. Returns
    the release id `current` pointed at immediately before (or `None` if
    it did not exist / was not a symlink).

    Implemented as `os.symlink` to a uniquely-named temporary link
    followed by `os.replace` over the real name, never a plain `ln -sfn`
    (unlink-then-symlink): the naive sequence has a window, however short,
    where `current` does not exist at all — a systemd unit restarting in
    exactly that window would resolve nothing. `os.replace` is a single
    atomic rename at the filesystem level, so `current` is always either
    the old target or the new one, never absent.

    Refuses (raises `HostCommandError`) if `current` exists and is not a
    symlink — a real directory at that path means a release was deployed
    by copying over `current` directly rather than through this function,
    and `os.replace`ing a symlink over a populated directory would either
    fail unpredictably or silently discard it; fail closed instead."""
    root = Path(release_root)
    target_dir = release_dir(root, release_id)
    if not target_dir.is_dir():
        raise HostCommandError(f"{target_dir} does not exist — cannot repoint current to it.")
    link = current_link(root)
    if link.exists() and not link.is_symlink():
        raise HostCommandError(
            f"{link} exists and is not a symlink — refusing to repoint it automatically. "
            "This usually means a release was installed by copying over `current` directly "
            "instead of through the release-copy step; resolve this by hand."
        )
    previous = read_current_target(root)
    tmp_link = link.with_name(f".{CURRENT_LINK_NAME}.tmp.{uuid.uuid4().hex}")
    relative_target = Path(RELEASES_DIRNAME) / release_id
    try:
        os.symlink(relative_target, tmp_link)
        os.replace(tmp_link, link)
    except OSError as exc:
        # Funnel filesystem failures into the same catchable error type
        # every other host operation uses (the same reasoning as
        # `run_release`'s QA-38 exception funnel) — a raw `OSError`
        # escaping here would bypass every `except HostCommandError` in
        # the deploy path and leave a release `pending` with no
        # auto-rollback attempted (QA-42's exact failure mode).
        tmp_link.unlink(missing_ok=True)
        raise HostCommandError(f"failed to repoint {link} to {relative_target}: {exc}") from exc
    return previous
