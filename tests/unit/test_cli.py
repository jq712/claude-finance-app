import datetime

from typer.testing import CliRunner

from finance_app.analytics.periods import month_bounds
from finance_app.cli.main import _resolve_period, app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_status_command_runs_without_a_database() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0


def test_resolve_period_defaults_to_the_current_month() -> None:
    today = datetime.date.today()
    assert _resolve_period(None) == month_bounds(today.year, today.month)


def test_resolve_period_parses_explicit_month() -> None:
    assert _resolve_period("2026-03") == month_bounds(2026, 3)


def test_resolve_period_rejects_malformed_month() -> None:
    result = runner.invoke(app, ["spending", "--month", "not-a-month"])
    assert result.exit_code != 0
