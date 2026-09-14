"""`finops` — the narrow production operations interface (handoff §10).

Exists specifically so autonomous engineering agents and the owner
diagnose and operate production through a fixed, auditable, semantic
command set instead of ad hoc `psql`/shell/SSH. Read commands
(`health`, `version`, `sync-status`, `db-status`, `migration-status`,
`backup-status`, `recent-errors`) connect as `finance_observer` — strictly
read-only, never a financial payload in the output (`ops/db.py`).
Write commands (`restart`, `deploy`, `rollback`) are the one place this
CLI is allowed to change production state, and they do so narrowly: a
release-directory/`systemctl` operation on the host (`ops/host.py`) plus a
bookkeeping row in `ops.releases` (`ops/release.py`, via `finance_app` —
the only role with write access there). None of these commands ever
execute arbitrary SQL or shell.

Every command supports `--json` for machine-readable output (evaluated by
autonomous tooling) alongside the default Rich human-readable rendering
(read by the owner). See docs/deployment.md for the release/rollback
sequence these commands implement (ADR-019's bare-metal, symlink-based
model — no Docker, no image, no registry).
"""

from __future__ import annotations

import json
import re

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from finance_app.config.settings import Settings, get_settings
from finance_app.db.models.ops import Release
from finance_app.db.session import session_scope
from finance_app.ops import release as release_ops
from finance_app.ops import status
from finance_app.ops.backup import latest_successful_backup
from finance_app.ops.db import observer_session_scope
from finance_app.ops.host import (
    DEFAULT_RELEASE_ROOT,
    HostCommandError,
    release_dir,
    release_is_installed,
    repoint_current,
    run_release,
    run_systemctl,
)

app = typer.Typer(
    name="finops",
    help="Narrow operations interface for the production application. No arbitrary SQL/shell.",
    no_args_is_help=True,
)

console = Console()

_RELEASE_ID_RE = re.compile(r"^[0-9a-f]{7,40}$")

# `recent-errors --limit` is user/agent-supplied and forwarded straight
# into a SQL LIMIT (QA-13) — bound it to a sane range so an out-of-range
# value is a clear usage error (exit 2) instead of an unhandled
# sqlalchemy.exc.DataError traceback.
_MAX_RECENT_ERRORS_LIMIT = 10_000


class RollbackTargetUnhealthyError(RuntimeError):
    """`_do_rollback`'s target release failed its own `probe_release` check
    (either the pre-repoint probe by path, or the post-restart probe
    through `current`) — raised instead of silently promoting it. A
    rollback must never report success for a target that cannot actually
    run."""


def _emit(data: dict | list, *, as_json: bool, title: str) -> None:
    """Renders `data` as a Rich table for interactive use (or raw JSON for
    `--json`). Every non-JSON value is wrapped in `rich.text.Text` rather
    than interpolated into an f-string Rich then parses as markup (QA-12)
    — `data` frequently carries strings sourced from the database
    (`ops.errors.message`, merchant/error text embedded in it), and per
    CLAUDE.md every such string is attacker-influenceable. Unescaped, a
    message containing `[red]...[/red]` silently restyles the operator's
    terminal, and an unmatched closing tag like `[/not-a-tag]` raises
    `rich.errors.MarkupError` and crashes the exact command an operator
    reaches for during an incident. `Text` is never interpreted as
    markup, so both cases render as inert literal text instead."""
    if as_json:
        print(json.dumps(data, default=str))  # noqa: T201 - machine-readable stdout, not logging
        return
    if isinstance(data, list):
        if not data:
            console.print(f"[dim]{title}: none[/dim]")
            return
        table = Table(title=title)
        for key in data[0]:
            table.add_column(key)
        for row in data:
            table.add_row(*(Text(str(v)) for v in row.values()))
        console.print(table)
        return
    table = Table(title=title)
    table.add_column("field")
    table.add_column("value")
    for key, value in data.items():
        table.add_row(key, Text(str(value)))
    console.print(table)


def _resolve_release_root(release_root: str | None, settings: Settings) -> str:
    return release_root if release_root is not None else settings.release_root


def _require_release_root_authority(release_root: str, settings: Settings) -> None:
    """Refuses to operate against the real production release root unless
    this process's own `Settings` were built from an explicitly-set
    `FINANCE_ENV_FILE` naming `FINANCE_ENV=production`
    (`config/settings.py`'s DSN guard, ADR-019).

    This is not filesystem permission enforcement — that boundary is
    `/opt/finance`'s Unix-user separation (ADR-019, ADR-007/ADR-010),
    entirely outside this process's control. It closes a narrower,
    concrete gap: without it, an operator who forgot to export
    `FINANCE_ENV_FILE` while pointing `--release-root` at `/opt/finance`
    would have deploy bookkeeping silently written to `finance_dev`
    (`session_scope`'s DSN comes from this same `Settings`) while the
    actual release-directory/`systemctl` operations acted on the real
    production tree — a bookkeeping/filesystem split with no error at
    all. Any `--release-root` other than the literal production default
    (e.g. every test's `tmp_path`) is unaffected."""
    if release_root == DEFAULT_RELEASE_ROOT and not settings.env_file_explicit:
        console.print(
            f"[red]refusing to operate against {DEFAULT_RELEASE_ROOT!r} (the production "
            "release root) without FINANCE_ENV_FILE explicitly set to a production env file "
            "(ADR-019) — this would record deploy bookkeeping against finance_dev while "
            "touching production's own release directory.[/red]"
        )
        raise typer.Exit(2)


@app.command()
def version(json_output: bool = typer.Option(False, "--json")) -> None:
    """Print the application version and the running release id."""
    data = status.version_info()
    if json_output:
        print(json.dumps(data))  # noqa: T201
    else:
        console.print(f"finance-app [bold]{data['app_version']}[/bold]")
        console.print(f"release: {data['release_id'] or '(not a release directory)'}")


@app.command(name="health")
def health_cmd(json_output: bool = typer.Option(False, "--json")) -> None:
    """Aggregate application/database/migration/sync/backup health.

    Exits non-zero when unhealthy, so this command is safe to use directly
    as a scripted health gate (e.g. `finops health || rollback`)."""
    settings = get_settings()
    with observer_session_scope() as session:
        result = status.aggregate_health(session, settings)
    _emit(result, as_json=json_output, title="health")
    if result["overall"] != "healthy":
        raise typer.Exit(1)


@app.command(name="sync-status")
def sync_status_cmd(json_output: bool = typer.Option(False, "--json")) -> None:
    """Most recent Plaid sync run and cursor state."""
    with observer_session_scope() as session:
        result = status.sync_status(session)
    _emit(result, as_json=json_output, title="sync-status")


@app.command(name="db-status")
def db_status_cmd(json_output: bool = typer.Option(False, "--json")) -> None:
    """Database reachability."""
    with observer_session_scope() as session:
        result = status.db_status(session)
    _emit(result, as_json=json_output, title="db-status")
    if result["status"] != "healthy":
        raise typer.Exit(1)


@app.command(name="migration-status")
def migration_status_cmd(json_output: bool = typer.Option(False, "--json")) -> None:
    """Compares the database's applied Alembic revision against the
    repository's head revision."""
    with observer_session_scope() as session:
        result = status.migration_status(session)
    _emit(result, as_json=json_output, title="migration-status")
    if result["status"] not in ("up_to_date",):
        raise typer.Exit(1)


@app.command(name="backup-status")
def backup_status_cmd(json_output: bool = typer.Option(False, "--json")) -> None:
    """Most recent backup and whether it has been restore-verified.

    "verified" requires both a successful backup *and* a successful
    restore-verification of that specific backup within the freshness
    window — see docs/backups.md."""
    with observer_session_scope() as session:
        result = status.backup_status(session)
    _emit(result, as_json=json_output, title="backup-status")
    if result["status"] != "verified":
        raise typer.Exit(1)


@app.command(name="recent-errors")
def recent_errors_cmd(
    limit: int = typer.Option(20, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recent sanitized `ops.errors` rows — never a financial payload."""
    if limit < 1 or limit > _MAX_RECENT_ERRORS_LIMIT:
        console.print(
            f"[red]--limit must be between 1 and {_MAX_RECENT_ERRORS_LIMIT}, got {limit}[/red]"
        )
        raise typer.Exit(2)
    with observer_session_scope() as session:
        result = status.recent_errors(session, limit=limit)
    _emit(result, as_json=json_output, title="recent-errors")


@app.command()
def restart(
    release_root: str = typer.Option(None, "--release-root", help="Override the release root."),
) -> None:
    """Re-run the currently-recorded release's smoke check via `finance
    selfcheck`, invoked *through* the `current` symlink.

    ADR-016 D2 (carried forward unchanged under ADR-019): until Milestone
    8's webhook, there is no long-running application process — production
    code runs only as systemd-timer-invoked one-shot commands. This is
    therefore a liveness re-check of the currently-recorded release, not a
    process restart; it changes nothing on disk or in `ops.releases`.

    Also refuses if the `current` symlink disagrees with what
    `ops.releases` bookkeeping believes is current
    (`status.release_topology`) — a disagreement a mutable symlink can
    produce (a hand-repointed link, an interrupted rollback) that a
    Docker image tag never could."""
    settings = get_settings()
    root = _resolve_release_root(release_root, settings)
    _require_release_root_authority(root, settings)
    with observer_session_scope() as session:
        topology = status.release_topology(session, release_root=root)
    release_id = topology["bookkeeping_current"]
    if release_id is None:
        console.print("[red]restart failed:[/red] no release is currently recorded")
        raise typer.Exit(1)
    if not topology["agrees"]:
        console.print(
            "[red]restart refused:[/red] bookkeeping says current is "
            f"{topology['bookkeeping_current']!r} but the `current` symlink resolves to "
            f"{topology['symlink_current']!r} — resolve the disagreement before proceeding."
        )
        raise typer.Exit(1)
    result = status.probe_release(
        release_id=release_id, release_root=root, via_current=True, run_release_fn=run_release
    )
    if result["status"] != "healthy":
        # Text(): `result` carries `probe_release`'s `detail`, sourced
        # from the release tree's own selfcheck stdout — QA-44, the
        # same class of hostile content `_emit` already guards against
        # for read commands (QA-12), reachable here too.
        console.print("[red]restart failed:[/red]", Text(str(result)))
        raise typer.Exit(1)
    console.print(f"[green]{release_id} is healthy[/green]")


@app.command()
def deploy(
    release_id: str = typer.Argument(..., help="Immutable Git SHA to deploy."),
    release_root: str = typer.Option(None, "--release-root", help="Override the release root."),
) -> None:
    """Deploy a release directory already installed under
    `<release_root>/releases/<sha>/` (ADR-019).

    Sequence: confirm the release directory is installed — never copy it;
    the release-copy mechanism is a separate step ADR-019 explicitly
    leaves as a follow-up decision. Refuse unless a recent successful
    backup is on record (ADR-019's release step 3, "back up finance_prod
    before touching it" — taking the backup is the owner's/timer's job;
    this only gates on one existing). Run the migration preflight from
    the *new* release's own tree. Probe the new release by path, before
    touching `current` at all (`deploy_health_check`) — deliberately
    ahead of ADR-019's literal step order (repoint -> restart ->
    health-check), which bare-metal makes possible in a way `docker pull`
    never did: probing an image required starting it, probing a release
    directory does not require it to be `current`. On success, atomically
    repoint `current`, restart configured systemd units, and re-probe
    *through* `current` before committing bookkeeping. On any failure,
    roll back automatically to the previous known-good release (ADR-008,
    carried forward unchanged)."""
    if not _RELEASE_ID_RE.match(release_id):
        # Text(): `release_id` is the rejected argument itself — the
        # validator has not yet confirmed it's even hex-shaped, so it may
        # contain anything, including a closing markup tag (QA-44).
        console.print("[red]not a valid release id (expected a Git SHA):[/red]", Text(release_id))
        raise typer.Exit(2)

    settings = get_settings()
    root = _resolve_release_root(release_root, settings)
    _require_release_root_authority(root, settings)

    if not release_is_installed(root, release_id):
        console.print(
            "[red]deploy failed:[/red] "
            f"{release_dir(root, release_id)} is not an installed release (missing directory "
            "or built virtualenv) — the release-copy step must run first."
        )
        raise typer.Exit(1)

    if latest_successful_backup() is None:
        console.print(
            "[red]deploy refused:[/red] no successful backup is on record — ADR-019 requires "
            "backing up finance_prod before touching it."
        )
        raise typer.Exit(1)

    artifact_ref = str(release_dir(root, release_id))

    with session_scope() as session:
        release = release_ops.start_deploy(
            session, release_id=release_id, artifact_ref=artifact_ref
        )
        release_row_id = release.id

    try:
        run_release(release_dir(root, release_id), "alembic", "upgrade", "head")
    except HostCommandError as exc:
        with session_scope() as session:
            release = session.get(Release, release_row_id)
            assert release is not None
            release_ops.mark_failed(session, release, reason=str(exc))
        # Text(): `HostCommandError`'s message includes up to 500 chars of
        # the release-tree command's own stderr, which is not
        # image-supplied but is still process output outside this
        # program's control — treated the same as every other probe-
        # sourced string per QA-44.
        console.print("[red]deploy failed to start:[/red]", Text(str(exc)))
        raise typer.Exit(1) from exc

    try:
        with observer_session_scope() as obs_session:
            health = status.deploy_health_check(
                obs_session,
                release_id=release_id,
                release_root=root,
                via_current=False,
                run_release_fn=run_release,
            )
    except typer.Exit:
        # `typer.Exit` is a `click.exceptions.Exit`, itself a
        # `RuntimeError` subclass — an ordinary `except Exception` below
        # would catch it too and relabel a deliberate CLI exit as "health
        # check raised". Nothing in this block raises it today, but the
        # distinction matters enough to keep explicit rather than rely on
        # that.
        raise
    except Exception as exc:  # noqa: BLE001 - QA-42: `run_release`/`probe_release`
        # already turn every subprocess failure they anticipate into
        # `HostCommandError`, which `deploy_health_check` handles
        # internally — this is the backstop for anything that still
        # escapes (e.g. the database going unreachable mid-check).
        # Without it, the release stays `pending` forever. Only the
        # exception's *class name* is recorded, never `str(exc)`/
        # `repr(exc)` (docs/security-model.md invariant 6) — some
        # exception types (e.g. a SQLAlchemy connection error) embed the
        # DSN, including its password, directly in their message.
        health = {
            "overall": "unhealthy",
            "database": "unknown",
            "migrations": "unknown",
            "application": {
                "status": "error",
                "reported_release_id": None,
                "detail": f"health check raised {type(exc).__name__}",
            },
        }

    if health["overall"] == "healthy":
        # Only now does `current` ever move: the new release has already
        # proven it selfchecks healthy from its own directory.
        try:
            repoint_current(root, release_id)
            if settings.production_units:
                run_systemctl(
                    "restart", *settings.production_units, prefix=settings.systemctl_prefix
                )
            post_restart = status.probe_release(
                release_id=release_id,
                release_root=root,
                via_current=True,
                run_release_fn=run_release,
            )
        except typer.Exit:
            raise
        except HostCommandError as exc:
            post_restart = {
                "status": "unreachable",
                "reported_release_id": None,
                "detail": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - same QA-42 backstop as above
            post_restart = {
                "status": "unreachable",
                "reported_release_id": None,
                "detail": f"post-restart promotion raised {type(exc).__name__}",
            }
        if post_restart["status"] != "healthy":
            health = {**health, "overall": "unhealthy", "application": post_restart}

    with session_scope() as session:
        release = session.get(Release, release_row_id)
        assert release is not None
        if health["overall"] == "healthy":
            release_ops.mark_healthy(session, release)
            deployed_ok = True
        else:
            release_ops.mark_failed(session, release, reason=f"post-deploy health: {health}")
            deployed_ok = False

    if deployed_ok:
        console.print(f"[green]deployed {release_id}[/green] ({artifact_ref})")
        if not settings.production_units:
            console.print(
                "[yellow]no production_units configured — systemd restart skipped.[/yellow]"
            )
        return

    # `health` wrapped in `Text` (never re-parsed as markup, same pattern
    # as `_emit` above) rather than interpolated into the markup string
    # directly: it can carry selfcheck output sourced from the release
    # tree's own stdout — attacker-influenceable per CLAUDE.md (Plaid
    # merchant text, model responses) and, since QA-36, potentially a
    # list of disagreeing candidate payloads verbatim.
    console.print("[red]post-deploy health check failed:[/red]", Text(str(health)))
    console.print("[yellow]rolling back automatically (ADR-008)...[/yellow]")
    try:
        _do_rollback(root, settings)
    except release_ops.NoPreviousReleaseError:
        console.print(
            "[red]no previous known-good release to roll back to — manual intervention "
            "required.[/red]"
        )
        raise typer.Exit(1) from None
    except RollbackTargetUnhealthyError as exc:
        # Text(): `exc`'s message embeds the rollback target's probe
        # payload — the same hostile-content class as `health` above
        # (QA-44), and arguably the worst place to crash: the deploy
        # already failed, the auto-rollback target failed too, and this
        # is the one line that tells the operator production needs
        # hands on it.
        console.print("[red]automatic rollback target is also unhealthy[/red]", Text(str(exc)))
        console.print("[red]manual intervention required.[/red]")
        raise typer.Exit(1) from None
    except release_ops.RollbackContendedError as exc:
        console.print(f"[red]automatic rollback could not complete safely:[/red] {exc}")
        raise typer.Exit(1) from None
    except typer.Exit:
        # See the matching guard above `deploy_health_check`'s catch-all:
        # `typer.Exit` is a `RuntimeError` subclass and must not be
        # relabeled by the blanket handler below.
        raise
    except Exception as exc:  # noqa: BLE001 - the failed release is already recorded
        # `failed` above regardless of what happens here; this only keeps
        # the auto-rollback *attempt* itself from crashing out with a raw
        # traceback (QA-42) if whatever broke the health check also
        # breaks the rollback probe. Only the exception's class name is
        # recorded — see the matching comment above `deploy_health_check`'s
        # own catch-all for why `str`/`repr` of an arbitrary exception is
        # not safe to surface.
        console.print(
            "[red]automatic rollback itself failed[/red]",
            f"({type(exc).__name__}) — manual intervention required.",
        )
        raise typer.Exit(1) from None
    raise typer.Exit(1)


@app.command()
def rollback(
    release_root: str = typer.Option(None, "--release-root", help="Override the release root."),
) -> None:
    """Roll back to the previously deployed known-good release (ADR-008).

    Confirms the previous release's directory still selfchecks healthy
    (both before and after the symlink swap), then atomically repoints
    `current` and flips `ops.releases` bookkeeping back to it — no copy,
    no rebuild, just what ADR-008 already guaranteed, translated to a
    symlink swap plus a systemd restart."""
    settings = get_settings()
    root = _resolve_release_root(release_root, settings)
    _require_release_root_authority(root, settings)
    try:
        released_id, bookkeeping_changed, symlink_changed = _do_rollback(root, settings)
    except release_ops.NoPreviousReleaseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except RollbackTargetUnhealthyError as exc:
        # Text(): see the matching guard in `deploy`'s auto-rollback
        # handler — `exc` embeds the target's probe payload (QA-44).
        console.print("[red]rollback target is not healthy:[/red]", Text(str(exc)))
        raise typer.Exit(1) from exc
    except release_ops.RollbackContendedError as exc:
        console.print(f"[red]rollback could not complete safely:[/red] {exc}")
        raise typer.Exit(1) from exc
    except HostCommandError as exc:
        console.print(
            "[red]rollback could not complete the symlink/restart step:[/red]", Text(str(exc))
        )
        raise typer.Exit(1) from exc
    if not bookkeeping_changed and not symlink_changed:
        # QA-48: the resolved target was already `current` in both the
        # database and on disk — a narrow, auditable production interface
        # must not report a state change it did not make.
        console.print(f"[yellow]{released_id} is already current — nothing to roll back.[/yellow]")
        return
    console.print(f"[green]rolled back to {released_id}[/green]")


def _do_rollback(release_root: str, settings: Settings) -> tuple[str, bool, bool]:
    """Shared rollback mechanics for `deploy`'s auto-rollback path and the
    `rollback` command. Returns `(release id now current,
    bookkeeping_changed, symlink_changed)`.

    Order mirrors `deploy`'s: the filesystem/systemd change (repoint,
    restart, re-probe through `current`) happens first and is verified;
    only once that succeeds does the database bookkeeping commit. A crash
    between the two leaves bookkeeping stale but never wrong in a way that
    blocks a retry — re-running rollback resolves the same target again,
    finds the symlink already correct (a no-op), and finishes the
    bookkeeping write it didn't reach last time.

    QA-41/QA-47 (row-identity promotion, probe-window race protection) are
    preserved unchanged in `release_ops.rollback` itself — only the
    Compose call this used to wrap is replaced by the symlink/systemd
    sequence above it."""
    with session_scope() as session:
        previous = release_ops.get_previous(session)
        if previous is None:
            raise release_ops.NoPreviousReleaseError("No previous known-good release is tracked.")
        target_release_id = previous.release_id
        # The row's primary key, not just its `release_id` (Git SHA):
        # `release_id` is deliberately not unique (a SHA can recur across
        # a failed attempt and a later successful one), so only the row
        # identity guarantees the release promoted below is the exact one
        # just probed, not merely one that happens to share its SHA.
        target_row_id = previous.id
        current_at_resolution = release_ops.get_current(session)
        expected_current_row_id = (
            current_at_resolution.id if current_at_resolution is not None else None
        )

    health = status.probe_release(
        release_id=target_release_id, release_root=release_root, run_release_fn=run_release
    )
    if health["status"] != "healthy":
        raise RollbackTargetUnhealthyError(f"{target_release_id}: {health}")

    symlink_previous = repoint_current(release_root, target_release_id)
    symlink_changed = symlink_previous != target_release_id
    if settings.production_units:
        run_systemctl("restart", *settings.production_units, prefix=settings.systemctl_prefix)
    post_restart = status.probe_release(
        release_id=target_release_id,
        release_root=release_root,
        via_current=True,
        run_release_fn=run_release,
    )
    if post_restart["status"] != "healthy":
        raise RollbackTargetUnhealthyError(f"{target_release_id} (post-restart): {post_restart}")

    # QA-41: promotes exactly the row that was just probed, never
    # re-derived. `expected_current_row_id` closes the sequential half of
    # the same race (QA-47) — see `release_ops.rollback`'s docstring.
    with session_scope() as session:
        release_ops.rollback(
            session, release_row_id=target_row_id, expected_current_row_id=expected_current_row_id
        )
    bookkeeping_changed = target_row_id != expected_current_row_id
    return target_release_id, bookkeeping_changed, symlink_changed


if __name__ == "__main__":
    app()
