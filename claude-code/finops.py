import typer

from finance_app import __version__

app = typer.Typer(
    name="finops",
    help="Narrow operations interface for the production application. No arbitrary SQL/shell.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
