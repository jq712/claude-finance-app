import datetime

from typer.testing import CliRunner

from finance_app.analytics.periods import month_bounds
from finance_app.cli.main import _resolve_period, app
from finance_app.db.session import dispose_engine

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_status_command_reports_unavailable_when_the_database_is_unreachable(monkeypatch) -> None:
    """Deterministic regardless of whether a local dev database happens to
    be running — points the app at a port nothing listens on, rather than
    relying on the ambient environment to already lack a database."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://nobody:nopass@localhost:1/does-not-exist"
    )
    dispose_engine()  # drop any pooled connection to the real dev database
    try:
        result = runner.invoke(app, ["status"])
    finally:
        dispose_engine()  # don't leak the bad URL's engine into later tests

    assert result.exit_code == 0
    assert "database unavailable:" in result.stdout


def test_resolve_period_defaults_to_the_current_month() -> None:
    today = datetime.date.today()
    assert _resolve_period(None) == month_bounds(today.year, today.month)


def test_resolve_period_parses_explicit_month() -> None:
    assert _resolve_period("2026-03") == month_bounds(2026, 3)


def test_resolve_period_rejects_malformed_month() -> None:
    result = runner.invoke(app, ["spending", "--month", "not-a-month"])
    assert result.exit_code != 0


def test_transactions_recent_rejects_negative_days() -> None:
    result = runner.invoke(app, ["transactions", "recent", "--days", "-1"])
    assert result.exit_code != 0


def test_transactions_recent_rejects_nonpositive_limit() -> None:
    result = runner.invoke(app, ["transactions", "recent", "--limit", "0"])
    assert result.exit_code != 0


def test_transactions_search_rejects_negative_days() -> None:
    result = runner.invoke(app, ["transactions", "search", "coffee", "--days", "-1"])
    assert result.exit_code != 0
