"""Integration tests for the `finance` CLI commands that need a real
database — spending/income/cashflow/budget/transactions. Seeds the same
golden dataset as tests/integration/test_analytics.py and asserts the
CLI's rendered output agrees with the analytics layer it wraps."""

import datetime
import re
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from finance_app.cli.main import app
from finance_app.db.repositories.transactions import TransactionFields, upsert
from tests.plaid_fixtures.synthetic import golden_month

pytestmark = pytest.mark.integration

MONTH_START = datetime.date(2026, 1, 1)
runner = CliRunner()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text_with_ansi: str) -> str:
    """Rich highlights numbers/etc. with ANSI codes even in a non-TTY
    CliRunner capture; strip them so exact-value assertions aren't broken
    by styling that has nothing to do with correctness."""
    return _ANSI_ESCAPE.sub("", text_with_ansi)


@pytest.fixture
def seeded_session(role_engine):
    engine = role_engine("finance_app")
    with Session(engine) as session:
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('cli-test-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'cli-test-account', 'Test Checking', 'depository') "
                "RETURNING id"
            ),
            {"item_id": item_id},
        ).scalar_one()
        for txn in golden_month(MONTH_START):
            upsert(
                session,
                TransactionFields(
                    plaid_transaction_id=txn.plaid_transaction_id,
                    account_id=account_id,
                    amount=txn.amount,
                    date=txn.date,
                    name=txn.name,
                    merchant_name=txn.merchant_name,
                    pending=txn.pending,
                    payment_channel=txn.payment_channel,
                    plaid_category=txn.plaid_category,
                    authorized_date=txn.authorized_date,
                ),
            )
        session.commit()
        try:
            yield session, account_id
        finally:
            session.rollback()
            session.execute(
                text("DELETE FROM plaid.transactions WHERE account_id = :a"), {"a": account_id}
            )
            session.execute(text("DELETE FROM plaid.accounts WHERE id = :a"), {"a": account_id})
            session.execute(text("DELETE FROM plaid.items WHERE id = :i"), {"i": item_id})
            session.execute(text("DELETE FROM finance.budgets"))
            session.commit()


def test_spending_command_shows_categories_and_total(seeded_session) -> None:
    result = runner.invoke(app, ["spending", "--month", "2026-01"])
    assert result.exit_code == 0
    assert "Rent" in result.stdout
    assert "1500.00" in result.stdout
    # Exact total, not just that a "Total:" line exists — 1500 rent + 100
    # cash advance + 87.32 groceries + 27.10 restaurants (netted refund) +
    # 15.99 subscription + 6.25 pending coffee, matching
    # tests/integration/test_analytics.py's by-category golden values.
    assert "Total: 1736.66" in _plain(result.stdout)


def test_spending_command_filters_by_category(seeded_session) -> None:
    result = runner.invoke(app, ["spending", "--month", "2026-01", "--category", "rent"])
    assert result.exit_code == 0
    assert "Rent" in result.stdout
    assert "Groceries" not in result.stdout


def test_income_command_reports_paycheck_total(seeded_session) -> None:
    result = runner.invoke(app, ["income", "--month", "2026-01"])
    assert result.exit_code == 0
    assert "Income 2026-01-01 to 2026-02-01: 3200.00" in result.stdout


def test_cashflow_command_reports_income_spending_and_net(seeded_session) -> None:
    result = runner.invoke(app, ["cashflow", "--month", "2026-01"])
    assert result.exit_code == 0
    assert "Income:" in result.stdout
    assert "Spending:" in result.stdout
    assert "Net:" in result.stdout


def test_budget_command_shows_no_active_budgets_by_default(seeded_session) -> None:
    result = runner.invoke(app, ["budget", "--month", "2026-01"])
    assert result.exit_code == 0
    assert "No active budgets." in result.stdout


def test_transactions_recent_lists_seeded_transactions(seeded_session) -> None:
    result = runner.invoke(app, ["transactions", "recent", "--days", "3650", "--limit", "50"])
    assert result.exit_code == 0
    assert "Riverside Property Management" in result.stdout


def test_transactions_search_finds_by_merchant(seeded_session) -> None:
    result = runner.invoke(app, ["transactions", "search", "diner", "--days", "3650"])
    assert result.exit_code == 0
    assert "Moonlight Diner" in result.stdout


def test_transactions_search_reports_no_matches(seeded_session) -> None:
    result = runner.invoke(app, ["transactions", "search", "nonexistent-xyz", "--days", "3650"])
    assert result.exit_code == 0
    assert "No transactions found." in result.stdout


def test_transactions_search_days_zero_still_includes_a_transaction_dated_today(
    seeded_session,
) -> None:
    """Regression test: `search`'s date window used to be computed
    independently of `recent`'s and was off by one, so `--days 0` silently
    excluded today's own transactions. Both commands must agree that
    `--days 0` means "just today"."""
    session, account_id = seeded_session
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="cli-test-today-txn",
            account_id=account_id,
            amount=Decimal("4.50"),
            date=datetime.date.today(),
            name="TODAY ONLY COFFEE SHOP",
        ),
    )
    session.commit()

    result = runner.invoke(app, ["transactions", "search", "today only", "--days", "0"])

    assert result.exit_code == 0
    assert "Today Only Coffee Shop".upper() in result.stdout.upper()


def test_status_command_reports_seeded_item_and_account(seeded_session) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Plaid items:" in result.stdout
