"""Adversarial regression tests for the `finops` CLI surface
(QA, Milestone 7 — handoff §10).

`finops` is the *narrow, auditable* production operations interface. Two
classes of hole found by adversarial review:

  - it renders attacker-influenceable database strings through Rich with
    markup enabled, so a hostile `ops.errors.message` can inject markup,
    emit raw ANSI, or crash the command outright;
  - it forwards `--limit` to SQL with no range validation.

All `xfail(strict=True)`; remove the marker once fixed.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from finance_app.cli.finops import app

pytestmark = pytest.mark.integration


def _healthy_selfcheck_run_compose(  # noqa: ANN001, ANN002, ANN003, ARG001
    compose_file, *args, env=None, **kwargs
):
    """Stands in for `ops.compose.run_compose`: every call succeeds, and a
    `finance selfcheck --json` run (ADR-016 D3 — `ops.status.probe_release`)
    reports the pinned `RELEASE_ID` as healthy, so tests using this fake
    exercise a deploy whose *release* is fine and are free to focus on
    whatever else they're actually testing."""
    if "selfcheck" in args:
        payload = json.dumps({"release_id": (env or {}).get("RELEASE_ID"), "overall": "healthy"})
        return subprocess.CompletedProcess(list(args), 0, payload + "\n", "")
    return subprocess.CompletedProcess(list(args), 0, "", "")

runner = CliRunner()


@pytest.fixture
def seeded_error(role_engine):
    """Insert one `ops.errors` row with a caller-supplied message, then
    clean up. `ops.errors.message`/`context` carry sanitized Plaid and
    provider text — attacker-influenceable per CLAUDE.md's rule to treat
    every string field as hostile."""
    engine = role_engine("finance_app")

    def _seed(message: str) -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ops.errors"))
            conn.execute(
                text("INSERT INTO ops.errors (category, message) VALUES ('plaid_sync', :m)"),
                {"m": message},
            )

    yield _seed
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.errors"))


def test_recent_errors_survives_a_hostile_markup_payload(seeded_error) -> None:
    seeded_error("sync failed for merchant [/not-a-tag]")

    result = runner.invoke(app, ["recent-errors"])

    assert result.exception is None, f"crashed with {type(result.exception).__name__}"
    assert result.exit_code == 0


def test_recent_errors_escapes_rather_than_interprets_markup(seeded_error) -> None:
    seeded_error("[red]ALL SYSTEMS HEALTHY[/red]")

    result = runner.invoke(app, ["recent-errors"])

    assert result.exit_code == 0
    # The literal tag text must survive to the terminal; if Rich consumed it
    # as markup, only "ALL SYSTEMS HEALTHY" (styled) is printed.
    assert "[red]" in result.output


@pytest.mark.parametrize("limit", ["-1", "999999999999999999999"])
def test_recent_errors_rejects_an_out_of_range_limit(limit: str) -> None:
    result = runner.invoke(app, ["recent-errors", "--limit", limit, "--json"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"unhandled {type(result.exception).__name__} for --limit {limit}"
    )
    assert result.exit_code == 2, "an out-of-range argument should be a usage error"


@pytest.fixture
def stale_sync_and_two_good_releases(role_engine):
    """A 48h-old successful sync (Plaid outage / item re-auth pending) plus
    a healthy A -> B release history, as `finops deploy` would find it."""
    import datetime

    engine = role_engine("finance_app")
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.execute(text("DELETE FROM ops.sync_runs"))
        conn.execute(text("DELETE FROM plaid.items WHERE plaid_item_id = 'qa-stale-item'"))
        item_id = conn.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, status) "
                "VALUES ('qa-stale-item', 'active') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO ops.sync_runs (item_id, run_type, status, started_at, finished_at) "
                "VALUES (:i, 'daily', 'success', :t, :t)"
            ),
            {"i": item_id, "t": old},
        )
        for release_id, st in (("a" * 7, "previous"), ("b" * 7, "current")):
            conn.execute(
                text(
                    "INSERT INTO ops.releases "
                    "(release_id, image_ref, status, health_check_status) "
                    "VALUES (:r, :img, :s, 'healthy')"
                ),
                {"r": release_id, "img": f"img:{release_id}", "s": st},
            )
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.execute(text("DELETE FROM ops.sync_runs"))
        conn.execute(text("DELETE FROM plaid.items WHERE plaid_item_id = 'qa-stale-item'"))


def test_a_pre_existing_stale_sync_does_not_auto_roll_back_an_unrelated_deploy(
    stale_sync_and_two_good_releases, monkeypatch
) -> None:
    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_compose", _healthy_selfcheck_run_compose)

    result = runner.invoke(app, ["deploy", "c" * 7])

    engine = stale_sync_and_two_good_releases
    with engine.begin() as conn:
        statuses = dict(
            conn.execute(text("SELECT release_id, status FROM ops.releases")).all()  # type: ignore[arg-type]
        )

    assert result.exit_code == 0, (
        f"deploy was rolled back over unrelated sync staleness: {statuses}"
    )
    assert statuses["c" * 7] == "current"
    assert statuses["b" * 7] == "previous"
