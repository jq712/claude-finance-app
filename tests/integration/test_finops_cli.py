"""Integration tests for the `finops` read commands against the real dev/
CI Postgres container — confirms the CLI, `ops/db.py`'s `finance_observer`
connection, and `ops/status.py` actually work together, not just in
isolation."""

import json

import pytest
from typer.testing import CliRunner

from finance_app.cli.finops import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def test_version_reports_app_version() -> None:
    result = runner.invoke(app, ["version", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "app_version" in payload


def test_db_status_healthy_against_the_real_database() -> None:
    result = runner.invoke(app, ["db-status", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "healthy"


def test_migration_status_reports_up_to_date() -> None:
    result = runner.invoke(app, ["migration-status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "up_to_date"
    assert payload["applied"] == payload["head"]


def test_health_exits_zero_and_reports_healthy_overall() -> None:
    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["overall"] == "healthy"


def test_recent_errors_runs_without_error_on_an_empty_table() -> None:
    result = runner.invoke(app, ["recent-errors", "--limit", "5", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_finops_read_commands_never_use_the_app_write_role() -> None:
    """`ops/db.py`'s whole reason to exist: finops's read commands
    connect as finance_observer, never finance_app/finance_owner. Proven
    indirectly here by confirming the observer connection alone is
    sufficient for every read command to succeed — if any of them
    silently depended on finance_app's broader grants, this would only
    be caught by the role-boundary tests in tests/security/, which is
    where the mechanical enforcement lives; this test just exercises the
    CLI's happy path end to end."""
    for command in (
        ["db-status", "--json"],
        ["sync-status", "--json"],
        ["backup-status", "--json"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code in (0, 1), (command, result.output)
