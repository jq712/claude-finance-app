"""Golden-dataset tests for `finance_app.analytics.*` (handoff §29
Milestone 3 exit criteria: "golden synthetic dataset returns exact
expected values"). Every total here was computed by hand from
`tests/plaid_fixtures/synthetic.golden_month` — see the module docstring
in each `analytics/*.py` for why refunds net against their category,
transfers/income are excluded from spending, and pending transactions are
included.
"""

import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.analytics.budgeting import get_budget_status
from finance_app.analytics.cashflow import calculate_cashflow
from finance_app.analytics.categories import classify
from finance_app.analytics.income import get_income_summary
from finance_app.analytics.periods import month_bounds, month_of, previous_month
from finance_app.analytics.recurring import find_recurring_transactions
from finance_app.analytics.spending import (
    compare_periods,
    get_spending_by_category,
    get_spending_summary,
)
from finance_app.db.repositories.transactions import TransactionFields, upsert
from tests.plaid_fixtures.synthetic import golden_month

pytestmark = pytest.mark.integration

MONTH_START = datetime.date(2026, 1, 1)


@pytest.fixture
def seeded_session(role_engine):
    """One Item/Account with `golden_month` loaded for January 2026."""
    engine = role_engine("finance_app")
    with Session(engine) as session:
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('analytics-test-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'analytics-test-account', 'Test Checking', 'depository') "
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
                text(
                    'DELETE FROM "user".transaction_category_overrides WHERE transaction_id IN '
                    "(SELECT id FROM plaid.transactions WHERE account_id = :a)"
                ),
                {"a": account_id},
            )
            session.execute(
                text("DELETE FROM plaid.transactions WHERE account_id = :a"), {"a": account_id}
            )
            session.execute(text("DELETE FROM plaid.accounts WHERE id = :a"), {"a": account_id})
            session.execute(text("DELETE FROM plaid.items WHERE id = :i"), {"i": item_id})
            session.execute(text("DELETE FROM finance.budgets"))
            session.commit()


def _month_range() -> tuple[datetime.date, datetime.date]:
    return month_bounds(MONTH_START.year, MONTH_START.month)


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------


def test_spending_by_category_matches_hand_computed_golden_values(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    by_category = get_spending_by_category(session, start=start, end=end)

    assert by_category == {
        "Rent": Decimal("1500.00"),
        "Cash Advance": Decimal("100.00"),
        "Groceries": Decimal("87.32"),
        "Restaurants": Decimal("27.10"),  # 42.10 purchase - 15.00 refund, netted
        "Subscription": Decimal("15.99"),
        "Coffee Shop": Decimal("6.25"),  # pending, included
    }
    assert "Payroll" not in by_category, "income must not appear as a spending category"
    assert "Transfer" not in by_category, "transfers must not appear as a spending category"


def test_spending_summary_matches_the_sum_of_the_category_breakdown(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    total = get_spending_summary(session, start=start, end=end)
    by_category = get_spending_by_category(session, start=start, end=end)

    assert total == Decimal("1736.66")
    assert total == sum(by_category.values(), Decimal("0"))


def test_spending_summary_excludes_periods_outside_the_range(seeded_session) -> None:
    session, _account_id = seeded_session
    next_month_start, next_month_end = month_bounds(2026, 2)

    total = get_spending_summary(session, start=next_month_start, end=next_month_end)

    assert total == Decimal("0")


# ---------------------------------------------------------------------------
# Income and cash flow
# ---------------------------------------------------------------------------


def test_income_summary_is_payroll_only(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    assert get_income_summary(session, start=start, end=end) == Decimal("3200.00")


def test_cashflow_net_and_savings_rate(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    result = calculate_cashflow(session, start=start, end=end)

    assert result.income == Decimal("3200.00")
    assert result.spending == Decimal("1736.66")
    assert result.net == Decimal("1463.34")
    assert result.savings_rate == Decimal("1463.34") / Decimal("3200.00")


def test_savings_rate_is_none_not_zero_when_income_is_zero(seeded_session) -> None:
    session, _account_id = seeded_session
    next_month_start, next_month_end = month_bounds(2026, 2)

    result = calculate_cashflow(session, start=next_month_start, end=next_month_end)

    assert result.income == Decimal("0")
    assert result.savings_rate is None


# ---------------------------------------------------------------------------
# Period comparison
# ---------------------------------------------------------------------------


def test_compare_periods_for_a_single_category(seeded_session) -> None:
    session, _account_id = seeded_session
    jan_start, jan_end = _month_range()
    feb_start, feb_end = month_bounds(2026, 2)

    comparison = compare_periods(
        session,
        period_a=(jan_start, jan_end),
        period_b=(feb_start, feb_end),
        category="Restaurants",
    )

    assert comparison.amount_a == Decimal("27.10")
    assert comparison.amount_b == Decimal("0")
    assert comparison.change == Decimal("-27.10")


def test_compare_periods_pct_change_is_none_when_period_a_is_zero(seeded_session) -> None:
    session, _account_id = seeded_session
    jan_start, jan_end = _month_range()
    feb_start, feb_end = month_bounds(2026, 2)

    comparison = compare_periods(
        session,
        period_a=(feb_start, feb_end),
        period_b=(jan_start, jan_end),
        category="Restaurants",
    )

    assert comparison.amount_a == Decimal("0")
    assert comparison.change_pct is None


# ---------------------------------------------------------------------------
# Category overrides change the effective view, never the raw fact
# ---------------------------------------------------------------------------


def test_override_moves_spend_from_one_category_to_another(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    txn_id = session.execute(
        text(
            "SELECT id FROM plaid.transactions WHERE plaid_transaction_id = "
            "'2026-01-01-restaurant-1'"
        )
    ).scalar_one()
    session.execute(
        text(
            'INSERT INTO "user".transaction_category_overrides (transaction_id, category) '
            "VALUES (:t, 'Client Dinners')"
        ),
        {"t": txn_id},
    )
    session.commit()

    by_category = get_spending_by_category(session, start=start, end=end)

    assert "Client Dinners" in by_category
    assert by_category["Client Dinners"] == Decimal("42.10")
    # The refund transaction is untouched, so "Restaurants" still exists
    # with just the -15.00 refund in it — a negative net, not a spend.
    assert by_category["Restaurants"] == Decimal("-15.00")

    raw_amount = session.execute(
        text("SELECT amount FROM plaid.transactions WHERE id = :t"), {"t": txn_id}
    ).scalar_one()
    assert raw_amount == Decimal("42.10"), "the override never touches the raw Plaid fact"


def test_override_reclassifies_a_transfer_as_real_spending(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    txn_id = session.execute(
        text(
            "SELECT id FROM plaid.transactions WHERE plaid_transaction_id = "
            "'2026-01-01-transfer-savings'"
        )
    ).scalar_one()
    session.execute(
        text(
            'INSERT INTO "user".transaction_category_overrides (transaction_id, category) '
            "VALUES (:t, 'Loan Payment')"
        ),
        {"t": txn_id},
    )
    session.commit()

    by_category = get_spending_by_category(session, start=start, end=end)

    assert by_category["Loan Payment"] == Decimal("500.00"), (
        "an override corrects a wrong Plaid transfer classification; the "
        "corrected category must count as real spending"
    )


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_budget_status_variance(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    session.execute(
        text(
            "INSERT INTO finance.budgets (category, monthly_amount) "
            "VALUES ('Restaurants', 50.00), ('Groceries', 50.00)"
        )
    )
    session.commit()

    statuses = {s.category: s for s in get_budget_status(session, start=start, end=end)}

    assert statuses["Restaurants"].actual_spent == Decimal("27.10")
    assert statuses["Restaurants"].remaining == Decimal("22.90")
    assert statuses["Restaurants"].over_budget is False

    assert statuses["Groceries"].actual_spent == Decimal("87.32")
    assert statuses["Groceries"].remaining == Decimal("-37.32")
    assert statuses["Groceries"].over_budget is True


def test_budget_with_no_matching_spend_reports_zero_not_missing(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    session.execute(
        text("INSERT INTO finance.budgets (category, monthly_amount) VALUES ('Travel', 200.00)")
    )
    session.commit()

    statuses = get_budget_status(session, start=start, end=end)

    assert len(statuses) == 1
    assert statuses[0].category == "Travel"
    assert statuses[0].actual_spent == Decimal("0")
    assert statuses[0].remaining == Decimal("200.00")


def test_inactive_budgets_are_excluded(seeded_session) -> None:
    session, _account_id = seeded_session
    start, end = _month_range()

    session.execute(
        text(
            "INSERT INTO finance.budgets (category, monthly_amount, active) "
            "VALUES ('Restaurants', 50.00, false)"
        )
    )
    session.commit()

    assert get_budget_status(session, start=start, end=end) == []


# ---------------------------------------------------------------------------
# Recurring detection
# ---------------------------------------------------------------------------


def test_recurring_merchant_detected_across_three_months(role_engine) -> None:
    engine = role_engine("finance_app")
    with Session(engine) as session:
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('recurring-test-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'recurring-test-account', 'Test Checking', 'depository') "
                "RETURNING id"
            ),
            {"item_id": item_id},
        ).scalar_one()
        try:
            for month in (11, 12):
                year = 2025
                upsert(
                    session,
                    TransactionFields(
                        plaid_transaction_id=f"recur-{year}-{month:02d}",
                        account_id=account_id,
                        amount=Decimal("15.99"),
                        date=datetime.date(year, month, 10),
                        name="STREAMBOX SUBSCRIPTION",
                        merchant_name="Streambox",
                        plaid_category=["Service", "Subscription"],
                    ),
                )
            upsert(
                session,
                TransactionFields(
                    plaid_transaction_id="recur-2026-01",
                    account_id=account_id,
                    amount=Decimal("15.99"),
                    date=datetime.date(2026, 1, 10),
                    name="STREAMBOX SUBSCRIPTION",
                    merchant_name="Streambox",
                    plaid_category=["Service", "Subscription"],
                ),
            )
            # A one-off purchase from a different merchant must not appear.
            upsert(
                session,
                TransactionFields(
                    plaid_transaction_id="one-off",
                    account_id=account_id,
                    amount=Decimal("42.00"),
                    date=datetime.date(2026, 1, 15),
                    name="RANDOM STORE",
                    merchant_name="Random Store",
                    plaid_category=["Shops"],
                ),
            )
            session.commit()

            recurring = find_recurring_transactions(session, as_of=datetime.date(2026, 1, 20))

            merchants = {r.merchant: r for r in recurring}
            assert "Streambox" in merchants
            assert merchants["Streambox"].occurrences == 3
            assert merchants["Streambox"].average_amount == Decimal("15.99")
            assert "Random Store" not in merchants
        finally:
            session.rollback()
            session.execute(
                text("DELETE FROM plaid.transactions WHERE account_id = :a"), {"a": account_id}
            )
            session.execute(text("DELETE FROM plaid.accounts WHERE id = :a"), {"a": account_id})
            session.execute(text("DELETE FROM plaid.items WHERE id = :i"), {"i": item_id})
            session.commit()


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def test_month_bounds_handles_december_year_rollover() -> None:
    start, end = month_bounds(2025, 12)
    assert start == datetime.date(2025, 12, 1)
    assert end == datetime.date(2026, 1, 1)


def test_month_of_and_previous_month() -> None:
    assert month_of(datetime.date(2026, 3, 15)) == month_bounds(2026, 3)
    assert previous_month(2026, 1) == (2025, 12)
    assert previous_month(2026, 6) == (2026, 5)


# ---------------------------------------------------------------------------
# Classification unit-level behavior (no DB needed)
# ---------------------------------------------------------------------------


def test_classify_prefers_override_over_raw_category() -> None:
    result = classify(
        override_category="Client Dinners",
        personal_finance_category={"primary": "FOOD_AND_DRINK", "detailed": "RESTAURANTS"},
        plaid_category=["Food and Drink", "Restaurants"],
    )
    assert result.effective_category == "Client Dinners"
    assert result.is_transfer is False
    assert result.is_income is False


def test_classify_detects_transfer_from_personal_finance_category() -> None:
    result = classify(
        override_category=None,
        personal_finance_category={
            "primary": "TRANSFER_OUT",
            "detailed": "TRANSFER_OUT_ACCOUNT_TRANSFER",
        },
        plaid_category=None,
    )
    assert result.is_transfer is True


def test_classify_falls_back_to_legacy_category_when_pfc_absent() -> None:
    result = classify(
        override_category=None,
        personal_finance_category=None,
        plaid_category=["Food and Drink", "Groceries"],
    )
    assert result.effective_category == "Groceries"
    assert result.is_transfer is False
    assert result.is_income is False


def test_classify_uncategorized_when_no_category_data_at_all() -> None:
    result = classify(override_category=None, personal_finance_category=None, plaid_category=None)
    assert result.effective_category == "Uncategorized"
