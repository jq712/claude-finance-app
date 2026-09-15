import datetime
import json

import typer
from anthropic import AnthropicError
from openai import OpenAIError
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from finance_app import __version__
from finance_app.agent.conversation import (
    append_turn,
    end_conversation,
    load_history,
    start_conversation,
)
from finance_app.agent.db import agent_session_scope
from finance_app.agent.loop import run_agent_turn
from finance_app.agent.prompts import system_prompt_for
from finance_app.agent.providers import build_provider
from finance_app.analytics.budgeting import get_budget_status
from finance_app.analytics.cashflow import calculate_cashflow
from finance_app.analytics.income import get_income_summary
from finance_app.analytics.periods import month_bounds
from finance_app.analytics.spending import get_spending_by_category, get_spending_summary
from finance_app.analytics.transactions import TransactionRecord, list_recent, search_transactions
from finance_app.config.settings import MissingProviderCredentialError, get_settings
from finance_app.db.models.agent import Conversation
from finance_app.db.models.plaid import Account, Item, SyncState
from finance_app.db.session import session_scope
from finance_app.ops.selfcheck import selfcheck as _selfcheck
from finance_app.plaid.sync import PlaidSyncError, run_daily_sync

app = typer.Typer(
    name="finance",
    help="Deterministic financial CLI. Useful even when the runtime LLM is unavailable.",
    no_args_is_help=True,
)

transactions_app = typer.Typer(help="List and search transactions.", no_args_is_help=True)
app.add_typer(transactions_app, name="transactions")

_MONTH_OPTION = typer.Option(
    None, "--month", help="Period as YYYY-MM. Defaults to the current calendar month."
)


def _resolve_period(month: str | None) -> tuple[datetime.date, datetime.date]:
    """`[start, end)` for `--month YYYY-MM`, or the current month if omitted."""
    if month is None:
        today = datetime.date.today()
        return month_bounds(today.year, today.month)
    try:
        year_str, month_str = month.split("-", 1)
        return month_bounds(int(year_str), int(month_str))
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM, e.g. 2026-01") from exc


def _print_transactions(records: list[TransactionRecord]) -> None:
    console = Console()
    if not records:
        console.print("No transactions found.")
        return
    table = Table()
    table.add_column("Date")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Amount", justify="right")
    for r in records:
        label = r.merchant_name or r.name
        if r.pending:
            label += " (pending)"
        table.add_row(str(r.date), label, r.effective_category, f"{r.amount:.2f}")
    console.print(table)


@app.command()
def status() -> None:
    """Report application, database, and sync health.

    Always exits 0, even when the database is unreachable — the CLI stays
    useful for basic inspection when the rest of the stack isn't up.
    """
    console = Console()
    console.print(f"finance-app [bold]{__version__}[/bold]")
    try:
        with session_scope() as session:
            item_count = session.execute(select(func.count()).select_from(Item)).scalar_one()
            account_count = session.execute(select(func.count()).select_from(Account)).scalar_one()
            sync_state = session.execute(select(SyncState)).scalars().first()
    except SQLAlchemyError as exc:  # database unreachable, wrong creds, etc.
        console.print(f"[yellow]database unavailable:[/yellow] {exc}")
        return

    console.print(f"Plaid items: {item_count}, accounts: {account_count}")
    if sync_state is None:
        console.print("sync: never run")
    else:
        console.print(
            f"sync: {sync_state.status}, last successful: {sync_state.last_successful_at}"
        )


@app.command()
def selfcheck(json_output: bool = typer.Option(False, "--json")) -> None:
    """Deterministic post-deploy smoke check (ADR-016 D3, adapted for
    ADR-019): database reachable, this release's migration head matches
    what's applied. Exits 0 iff `overall == "healthy"`; never 0 on a
    database or migration problem, unlike `finance status` above, which
    always exits 0 so it stays useful for basic inspection. `finops
    deploy` runs this via `<release>/.venv/bin/finance selfcheck --json`
    against the exact release tree being deployed
    (`ops.status.probe_release`) — the `finops` control-plane process
    itself may be running from a different release and can never answer
    this."""
    result = _selfcheck()
    if json_output:
        print(json.dumps(result))
    else:
        console = Console()
        console.print(f"release: {result['release_id']}, app: {result['app_version']}")
        console.print(f"database: {result['database']['status']}")
        console.print(
            f"migrations: {result['migrations']['status']} "
            f"(applied={result['migrations']['applied']}, head={result['migrations']['head']})"
        )
        console.print(f"overall: {result['overall']}")
    if result["overall"] != "healthy":
        raise typer.Exit(1)


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
def spending(
    month: str | None = _MONTH_OPTION,
    category: str | None = typer.Option(
        None, "--category", help="Show only this effective category (case-insensitive)."
    ),
) -> None:
    """Spending totals for a period, broken down by effective category."""
    start, end = _resolve_period(month)
    with session_scope() as session:
        by_category = get_spending_by_category(session, start=start, end=end)
        total = get_spending_summary(session, start=start, end=end)

    if category is not None:
        target = category.casefold()
        by_category = {c: a for c, a in by_category.items() if c.casefold() == target}

    console = Console()
    table = Table(title=f"Spending {start} to {end}")
    table.add_column("Category")
    table.add_column("Amount", justify="right")
    for cat, amount in by_category.items():
        table.add_row(cat, f"{amount:.2f}")
    console.print(table)
    console.print(f"Total: {total:.2f}")


@app.command()
def income(month: str | None = _MONTH_OPTION) -> None:
    """Total income for a period."""
    start, end = _resolve_period(month)
    with session_scope() as session:
        total = get_income_summary(session, start=start, end=end)
    typer.echo(f"Income {start} to {end}: {total:.2f}")


@app.command()
def cashflow(month: str | None = _MONTH_OPTION) -> None:
    """Income, spending, and net cash flow for a period."""
    start, end = _resolve_period(month)
    with session_scope() as session:
        result = calculate_cashflow(session, start=start, end=end)

    console = Console()
    console.print(f"Cash flow {start} to {end}")
    console.print(f"  Income:   {result.income:.2f}")
    console.print(f"  Spending: {result.spending:.2f}")
    console.print(f"  Net:      {result.net:.2f}")
    rate = result.savings_rate
    console.print(f"  Savings rate: {'n/a' if rate is None else f'{rate:.1%}'}")


@app.command()
def budget(month: str | None = _MONTH_OPTION) -> None:
    """Active budgets against actual spending for a period."""
    start, end = _resolve_period(month)
    with session_scope() as session:
        statuses = get_budget_status(session, start=start, end=end)

    console = Console()
    if not statuses:
        console.print("No active budgets.")
        return

    table = Table(title=f"Budgets {start} to {end}")
    table.add_column("Category")
    table.add_column("Budget", justify="right")
    table.add_column("Spent", justify="right")
    table.add_column("Remaining", justify="right")
    for s in statuses:
        style = "red" if s.over_budget else None
        table.add_row(
            s.category,
            f"{s.monthly_amount:.2f}",
            f"{s.actual_spent:.2f}",
            f"{s.remaining:.2f}",
            style=style,
        )
    console.print(table)


@transactions_app.command("recent")
def transactions_recent(
    days: int = typer.Option(30, min=0, help="Look back this many days."),
    limit: int = typer.Option(20, min=1, help="Maximum rows to show."),
) -> None:
    """The most recent transactions, newest first."""
    with session_scope() as session:
        records = list_recent(session, as_of=datetime.date.today(), days=days, limit=limit)
    _print_transactions(records)


@transactions_app.command("search")
def transactions_search(
    query: str = typer.Argument(..., help="Case-insensitive substring to match name/merchant."),
    days: int = typer.Option(365, min=0, help="Look back this many days."),
    limit: int = typer.Option(20, min=1, help="Maximum rows to show."),
) -> None:
    """Search transactions by name/merchant substring."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days)
    end = today + datetime.timedelta(days=1)  # half-open; --days 0 still includes today
    with session_scope() as session:
        records = search_transactions(session, query=query, start=start, end=end, limit=limit)
    _print_transactions(records)


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@app.command()
def chat() -> None:
    """Interactive conversation with the runtime financial agent.

    Provider selected by AGENT_PROVIDER (ADR-014). Every other `finance`
    command works without the LLM or its credential; this is the one
    command that needs both a reachable database and the active
    provider's API key, and fails fast with a clear message if either is
    missing rather than deep inside a conversation turn.
    """
    console = Console()
    settings = get_settings()
    try:
        provider = build_provider(settings)
    except MissingProviderCredentialError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    system_prompt = system_prompt_for(provider.provider_name)

    try:
        with agent_session_scope() as session:
            conversation_id = start_conversation(session).id
    except SQLAlchemyError as exc:
        typer.secho(f"database unavailable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    console.print(f"finance chat ({provider.provider_name}). Type 'exit' or Ctrl-D to quit.")
    try:
        while True:
            try:
                user_message = console.input("[bold]you>[/bold] ").strip()
            except EOFError:
                console.print()
                break
            if not user_message:
                continue
            if user_message.casefold() in {"exit", "quit"}:
                break

            with agent_session_scope() as session:
                history = load_history(session, conversation_id=conversation_id)
                try:
                    reply = run_agent_turn(
                        session,
                        provider=provider,
                        system_prompt=system_prompt,
                        history=history,
                        user_message=user_message,
                        conversation_id=conversation_id,
                    )
                except (OpenAIError, AnthropicError) as exc:
                    console.print(f"[red]agent provider error:[/red] {exc}")
                    continue
                append_turn(
                    session,
                    conversation_id=conversation_id,
                    user_message=user_message,
                    assistant_message=reply,
                )
            console.print(f"[bold cyan]agent>[/bold cyan] {reply}")
    finally:
        with agent_session_scope() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                end_conversation(session, conversation)


if __name__ == "__main__":
    app()
