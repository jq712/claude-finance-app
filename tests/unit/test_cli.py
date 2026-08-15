from typer.testing import CliRunner

from finance_app.cli.main import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_status_command_runs_without_a_database() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
