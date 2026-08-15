import typer

from finance_app import __version__
from finance_app.plaid.sync import PlaidSyncError, run_daily_sync

app = typer.Typer(
    name="finance",
    help="Deterministic financial CLI. Useful even when the runtime LLM is unavailable.",
    no_args_is_help=True,
)


@app.command()
def status() -> None:
    """Report application/database/sync health. Placeholder pending Milestone 3+."""
    typer.echo(f"finance-app {__version__}: database and Plaid sync are wired up.")


@app.command()
def sync() -> None:
    """Run `/transactions/sync` to completion for the single connected Item.

    Safe to interrupt and re-run: the durable cursor in `plaid.sync_state`
    only advances alongside the database writes it describes, so a killed
    run resumes from the last committed page rather than losing or
    duplicating data. See docs/plaid-sync.md.
    """
    try:
        summary = run_daily_sync()
    except PlaidSyncError as exc:
        typer.secho(f"sync failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"sync run {summary.run_id}: {summary.status} — "
        f"{summary.added_count} added, {summary.modified_count} modified, "
        f"{summary.removed_count} removed across {summary.pages} page(s)."
    )


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
