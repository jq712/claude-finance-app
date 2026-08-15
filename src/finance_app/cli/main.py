import typer

from finance_app import __version__

app = typer.Typer(
    name="finance",
    help="Deterministic financial CLI. Useful even when the runtime LLM is unavailable.",
    no_args_is_help=True,
)


@app.command()
def status() -> None:
    """Report application/database/sync health. Placeholder pending Milestone 1+."""
    typer.echo(f"finance-app {__version__}: engineering rails only, no database wired up yet.")


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
