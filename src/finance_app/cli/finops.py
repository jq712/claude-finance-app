"""`finops` — the narrow production operations interface (handoff §10).

Exists specifically so autonomous engineering agents and the owner
diagnose and operate production through a fixed, auditable, semantic
command set instead of ad hoc `psql`/shell/SSH. Read commands
(`health`, `version`, `sync-status`, `db-status`, `migration-status`,
`backup-status`, `recent-errors`) connect as `finance_observer` — strictly
read-only, never a financial payload in the output (`ops/db.py`).
Write commands (`restart`, `deploy`, `rollback`) are the one place this
CLI is allowed to change production state, and they do so narrowly: a
`docker compose` operation on the stack already running on the host
(`ops/compose.py`) plus a bookkeeping row in `ops.releases`
(`ops/release.py`, via `finance_app` — the only role with write access
there). None of these commands ever execute arbitrary SQL or shell.

Every command supports `--json` for machine-readable output (evaluated by
autonomous tooling) alongside the default Rich human-readable rendering
(read by the owner). See docs/deployment.md for the release/rollback
sequence these commands implement.
"""

from __future__ import annotations

import json
import re

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from finance_app.config.settings import get_settings
from finance_app.db.models.ops import Release
from finance_app.db.session import session_scope
from finance_app.ops import release as release_ops
from finance_app.ops import status
from finance_app.ops.compose import DEFAULT_COMPOSE_FILE, ComposeError, run_compose
from finance_app.ops.db import observer_session_scope

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


@app.command()
def version(json_output: bool = typer.Option(False, "--json")) -> None:
    """Print the application version and the running release id."""
    data = status.version_info()
    if json_output:
        print(json.dumps(data))  # noqa: T201
    else:
        console.print(f"finance-app [bold]{data['app_version']}[/bold]")
        console.print(f"release: {data['release_id'] or '(not a release image)'}")


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
    compose_file: str = typer.Option(DEFAULT_COMPOSE_FILE, "--compose-file"),
) -> None:
    """Restart the `app` container via `docker compose restart app`.

    Must run on the host already running the compose stack (the VPS) —
    this is the narrow, auditable substitute for ad hoc shell/SSH access
    per ADR-007."""
    try:
        run_compose(compose_file, "restart", "app")
    except ComposeError as exc:
        console.print(f"[red]restart failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print("[green]app restarted[/green]")


@app.command()
def deploy(
    release_id: str = typer.Argument(..., help="Immutable Git SHA to deploy (image tag)."),
    compose_file: str = typer.Option(DEFAULT_COMPOSE_FILE, "--compose-file"),
) -> None:
    """Deploy an image already published to the registry, by Git SHA.

    Sequence (ADR-008): pull the tagged image, run pending migrations as
    an explicit preflight step (ADR-008's "migration preflight" — nothing
    in this path used to actually run `alembic upgrade head` against the
    new image, so a deploy that needed a migration would come up against
    a stale schema), bring the stack up under that tag, verify health via
    `deploy_health_check` (deliberately narrower than `finops health`'s
    `aggregate_health` — see that function's docstring, QA-14: a
    pre-existing Plaid outage or unverified backup has nothing to do with
    whether *this* deploy is healthy, and conflating the two used to
    auto-rollback every deploy during an unrelated outage), and record
    the release as `current` — or, on a failed health check, automatically
    roll back to the previous known-good release and record this attempt
    as `failed`. Run this on the VPS after CI has published
    `<container_image_repo>:<release_id>` to GHCR and the
    production-deploy approval gate has passed (docs/deployment.md)."""
    if not _RELEASE_ID_RE.match(release_id):
        console.print(f"[red]not a valid release id (expected a Git SHA):[/red] {release_id}")
        raise typer.Exit(2)

    settings = get_settings()
    image_ref = f"{settings.container_image_repo}:{release_id}"
    env = {"RELEASE_ID": release_id}

    with session_scope() as session:
        release = release_ops.start_deploy(session, release_id=release_id, image_ref=image_ref)
        release_row_id = release.id

    try:
        run_compose(compose_file, "pull", "app", env=env)
        # Migrations run via the narrowly-scoped `migrate` service
        # (finance_migrator only — full DDL authority the long-running
        # `app` service must never hold, finding 1), not by overloading
        # `app`'s own definition for a one-shot job.
        run_compose(compose_file, "--profile", "migrate", "run", "--rm", "-T", "migrate", env=env)
        run_compose(compose_file, "up", "-d", "app", env=env)
    except ComposeError as exc:
        with session_scope() as session:
            release = session.get(Release, release_row_id)
            assert release is not None
            release_ops.mark_failed(session, release, reason=str(exc))
        console.print(f"[red]deploy failed to start:[/red] {exc}")
        raise typer.Exit(1) from exc

    with observer_session_scope() as obs_session:
        health = status.deploy_health_check(
            obs_session, compose_file=compose_file, run_compose_fn=run_compose
        )

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
        console.print(f"[green]deployed {release_id}[/green] ({image_ref})")
        return

    console.print(f"[red]post-deploy health check failed:[/red] {health}")
    console.print("[yellow]rolling back automatically (ADR-008)...[/yellow]")
    try:
        _do_rollback(compose_file)
    except release_ops.NoPreviousReleaseError:
        console.print(
            "[red]no previous known-good release to roll back to — manual intervention "
            "required.[/red]"
        )
        raise typer.Exit(1) from None
    raise typer.Exit(1)


@app.command()
def rollback(
    compose_file: str = typer.Option(DEFAULT_COMPOSE_FILE, "--compose-file"),
) -> None:
    """Roll back to the previously deployed known-good release (ADR-008).

    No image build, no registry fetch beyond what's already local — just
    bringing the stack up under the previous release's tag."""
    try:
        released_id = _do_rollback(compose_file)
    except release_ops.NoPreviousReleaseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]rolled back to {released_id}[/green]")


def _do_rollback(compose_file: str) -> str:
    """Shared rollback mechanics for `deploy`'s auto-rollback path and the
    `rollback` command. Returns the release id now running."""
    with session_scope() as session:
        previous = release_ops.get_previous(session)
        if previous is None:
            raise release_ops.NoPreviousReleaseError("No previous known-good release is tracked.")
        target_release_id = previous.release_id

    env = {"RELEASE_ID": target_release_id}
    run_compose(compose_file, "up", "-d", "app", env=env)

    with session_scope() as session:
        release_ops.rollback(session)
    return target_release_id


if __name__ == "__main__":
    app()
