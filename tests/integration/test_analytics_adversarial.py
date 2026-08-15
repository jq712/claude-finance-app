"""Adversarial tests for `finance_app.analytics.*`.

These attacked the Milestone 3 implementation rather than confirmed it.
Four real defects were found and fixed in a follow-up commit (override
transfer/income sentinel matching was case-sensitive and, for transfers,
had no override escape hatch at all; recurring detection split one
merchant across `merchant_name`/`name` label drift; budget-category
matching was case-sensitive) — those tests now assert the *fixed*
behavior and pass. Two findings are accepted, documented limitations
rather than bugs fixed here — see docs/analytics.md's Known limitations —
and their tests assert *current* behavior so a future change to that
behavior is a deliberate, reviewed decision, not a silent regression.
"""

import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.analytics.budgeting import get_budget_status
from finance_app.analytics.categories import classify
from finance_app.analytics.income import get_income_summary
from finance_app.analytics.periods import month_bounds
from finance_app.analytics.recurring import find_recurring_transactions
from finance_app.analytics.spending import compare_periods, get_spending_by_category
from finance_app.db.repositories.transactions import TransactionFields, mark_removed, upsert

pytestmark = pytest.mark.integration


@pytest.fixture
def item_account(role_engine):
    """A bare Item/Account, no transactions — each test seeds its own."""
    engine = role_engine("finance_app")
    with Session(engine) as session:
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('adv-analytics-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'adv-analytics-account', 'Test Checking', 'depository') "
                "RETURNING id"
            ),
            {"item_id": item_id},
        ).scalar_one()
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


# ---------------------------------------------------------------------------
# 1. FIXED — the override branch in classify() now has a transfer escape
#    hatch symmetric with income's, and both match case-insensitively.
# ---------------------------------------------------------------------------


def test_override_named_transfer_is_excluded_as_a_transfer() -> None:
    """A user who corrects a transaction's category to the literal string
    "Transfer" (a plausible action: they see Plaid mis-tagged it and want
    to flag it as an internal transfer between their own accounts) must
    get exactly that: the override branch now has the same
    transfer-sentinel escape hatch the income branch always had."""
    result = classify(
        override_category="Transfer",
        personal_finance_category=None,
        plaid_category=["Food and Drink", "Restaurants"],
    )
    assert result.is_transfer is True


def test_override_named_transfer_appears_as_spending_end_to_end(item_account) -> None:
    session, account_id = item_account
    start, end = month_bounds(2026, 1)

    txn = upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-transfer-override",
            account_id=account_id,
            amount=Decimal("500.00"),
            date=datetime.date(2026, 1, 10),
            name="MOVE TO SAVINGS",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
    )
    session.execute(
        text(
            'INSERT INTO "user".transaction_category_overrides (transaction_id, category) '
            "VALUES (:t, 'Transfer')"
        ),
        {"t": txn.id},
    )
    session.commit()

    by_category = get_spending_by_category(session, start=start, end=end)

    assert "Transfer" not in by_category, (
        "an override of 'Transfer' should exclude the transaction from "
        "spending, matching how a raw Plaid 'Transfer' category is excluded"
    )


def test_override_income_label_is_case_insensitive() -> None:
    """A lowercase "income" override (plausible from a free-text field, or
    an agent tool normalizing case) must match the "Income" sentinel just
    as reliably as exact casing does."""
    result = classify(
        override_category="income",
        personal_finance_category=None,
        plaid_category=None,
    )
    assert result.is_income is True


def test_override_income_lowercase_pollutes_spending_total_end_to_end(item_account) -> None:
    session, account_id = item_account
    start, end = month_bounds(2026, 1)

    txn = upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-income-lowercase",
            account_id=account_id,
            amount=Decimal("-3200.00"),
            date=datetime.date(2026, 1, 3),
            name="ACME CORP PAYROLL",
            plaid_category=["Shops"],  # Plaid mis-tagged it; user corrects
        ),
    )
    session.execute(
        text(
            'INSERT INTO "user".transaction_category_overrides (transaction_id, category) '
            "VALUES (:t, 'income')"
        ),
        {"t": txn.id},
    )
    session.commit()

    by_category = get_spending_by_category(session, start=start, end=end)
    income = get_income_summary(session, start=start, end=end)

    assert income == Decimal("3200.00"), "paycheck was silently not counted as income"
    assert "income" not in by_category


# ---------------------------------------------------------------------------
# 2. FIXED — find_recurring_transactions now backfills merchant_name
#    across occurrences sharing the same raw `name`.
# ---------------------------------------------------------------------------


def test_recurring_detection_resolves_merchant_name_and_name_label_drift(item_account) -> None:
    """Plaid data-quality gap: the same subscription sometimes arrives with
    `merchant_name` populated, sometimes with only `name`. Real-world this
    happens (Plaid's merchant enrichment is probabilistic and can flip
    month to month for the same billing descriptor). `find_recurring_transactions`
    now resolves every occurrence sharing a raw `name` to the same grouping
    key by preferring any `merchant_name` seen for that `name` in the
    window, so this one real recurring charge is detected as one series."""
    session, account_id = item_account

    # Three real occurrences of the same subscription across three months —
    # a human looking at a statement would call this obviously recurring —
    # but the merchant_name/name split means neither derived key reaches
    # min_occurrences=3 individually.
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-recur-nov",
            account_id=account_id,
            amount=Decimal("15.99"),
            date=datetime.date(2025, 11, 10),
            name="STREAMBOX SUBSCRIPTION",
            merchant_name=None,
            plaid_category=["Service", "Subscription"],
        ),
    )
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-recur-dec",
            account_id=account_id,
            amount=Decimal("15.99"),
            date=datetime.date(2025, 12, 10),
            name="STREAMBOX SUBSCRIPTION",
            merchant_name=None,
            plaid_category=["Service", "Subscription"],
        ),
    )
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-recur-jan",
            account_id=account_id,
            amount=Decimal("15.99"),
            date=datetime.date(2026, 1, 10),
            name="STREAMBOX SUBSCRIPTION",
            merchant_name="Streambox",  # Plaid enrichment kicks in this month
            plaid_category=["Service", "Subscription"],
        ),
    )
    session.commit()

    recurring = find_recurring_transactions(
        session,
        as_of=datetime.date(2026, 1, 20),
        lookback_months=3,
        min_occurrences=3,
    )
    merchants = {r.merchant: r for r in recurring}

    # All three occurrences resolve to "Streambox" — the merchant_name
    # seen in January is now backfilled onto the November/December
    # occurrences that only had `name`, so one series of 3 is detected.
    assert "Streambox" in merchants
    assert merchants["Streambox"].occurrences == 3
    assert "STREAMBOX SUBSCRIPTION" not in merchants, "must not remain split into two series"


# ---------------------------------------------------------------------------
# 3. Category-label matching: case-sensitivity FIXED in both
#    compare_periods and get_budget_status. Genuine label-source drift
#    (legacy taxonomy vs. personal_finance_category producing different
#    strings for the same real category) is an accepted, documented
#    limitation — see docs/analytics.md.
# ---------------------------------------------------------------------------


def test_compare_periods_accepted_limitation_label_source_drift_between_periods(
    item_account,
) -> None:
    """Known, accepted limitation (docs/analytics.md) — not fixed here.
    Same real-world category (restaurant spend), but Plaid reported it via
    the legacy `plaid_category` list in January and switched to
    `personal_finance_category` in February (a realistic mid-history Plaid
    taxonomy migration). `_raw_label` derives a genuinely different string
    in each case — this is not a case-sensitivity issue (that part is
    fixed; see `compare_periods`' `.casefold()` matching) but two
    different label strings for what a human would call the same
    category. `compare_periods` scoped to a category silently treats
    February as zero spend rather than reconciling or erroring. Fixing
    this properly needs a canonical category-taxonomy mapping, which is
    out of scope for Milestone 3; this test pins the current behavior so a
    future change is a deliberate decision, not a silent regression."""
    session, account_id = item_account
    jan_start, jan_end = month_bounds(2026, 1)
    feb_start, feb_end = month_bounds(2026, 2)

    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-label-jan",
            account_id=account_id,
            amount=Decimal("40.00"),
            date=datetime.date(2026, 1, 15),
            name="MOONLIGHT DINER",
            merchant_name="Moonlight Diner",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
    )
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-label-feb",
            account_id=account_id,
            amount=Decimal("45.00"),
            date=datetime.date(2026, 2, 15),
            name="MOONLIGHT DINER",
            merchant_name="Moonlight Diner",
            personal_finance_category={
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_RESTAURANTS",
            },
        ),
    )
    session.commit()

    by_category_jan = get_spending_by_category(session, start=jan_start, end=jan_end)
    by_category_feb = get_spending_by_category(session, start=feb_start, end=feb_end)

    # The same real merchant/category now has two different effective
    # labels purely because of which Plaid field happened to be populated.
    assert "Restaurants" in by_category_jan
    assert "Restaurants" not in by_category_feb
    assert "Food And Drink Restaurants" in by_category_feb

    comparison = compare_periods(
        session,
        period_a=(jan_start, jan_end),
        period_b=(feb_start, feb_end),
        category="Restaurants",
    )

    # Accepted limitation: real restaurant spend went from $40 to $45
    # (+$5), but the label drift makes compare_periods report February as
    # $0 spent in "Restaurants" — a -$40 swing that reads as spending
    # having stopped, when it actually increased. See docs/analytics.md.
    assert comparison.amount_b == Decimal("0"), (
        "documents the accepted label-source-drift limitation — if this "
        "starts failing, either the limitation was fixed (update "
        "docs/analytics.md) or something else changed unexpectedly"
    )


def test_budget_status_matches_category_label_regardless_of_case(item_account) -> None:
    """A budget category entered with different casing than the label
    analytics derives (e.g. user typed "restaurants" in the budget CLI,
    derivation produces "Restaurants") must still match — `get_budget_status`
    now compares case-insensitively."""
    session, account_id = item_account
    start, end = month_bounds(2026, 1)

    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-budget-case",
            account_id=account_id,
            amount=Decimal("60.00"),
            date=datetime.date(2026, 1, 15),
            name="MOONLIGHT DINER",
            merchant_name="Moonlight Diner",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
    )
    session.execute(
        text("INSERT INTO finance.budgets (category, monthly_amount) VALUES ('restaurants', 50.00)")
    )
    session.commit()

    statuses = {s.category: s for s in get_budget_status(session, start=start, end=end)}

    assert statuses["restaurants"].actual_spent == Decimal("60.00")
    assert statuses["restaurants"].over_budget is True


# ---------------------------------------------------------------------------
# 4. Tombstone + resurrection: a stale override silently re-applies the
#    instant Plaid's sync replays an `added`/`modified` page for a
#    previously-removed transaction id, with no audit trail of that having
#    happened.
# ---------------------------------------------------------------------------


def test_stale_override_silently_reapplies_when_a_removed_transaction_is_resurrected(
    item_account,
) -> None:
    session, account_id = item_account
    start, end = month_bounds(2026, 1)

    txn = upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-tombstone-resurrect",
            account_id=account_id,
            amount=Decimal("42.10"),
            date=datetime.date(2026, 1, 5),
            name="MOONLIGHT DINER",
            merchant_name="Moonlight Diner",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
    )
    session.execute(
        text(
            'INSERT INTO "user".transaction_category_overrides (transaction_id, category) '
            "VALUES (:t, 'Client Dinners')"
        ),
        {"t": txn.id},
    )
    session.commit()

    by_category_before = get_spending_by_category(session, start=start, end=end)
    assert by_category_before.get("Client Dinners") == Decimal("42.10")

    # Plaid reports the transaction removed (e.g. it was a duplicate or
    # provisional hold that got reversed) — tombstone it, never a hard
    # delete, per handoff/CLAUDE.md.
    mark_removed(session, "adv-tombstone-resurrect")
    session.commit()

    by_category_removed = get_spending_by_category(session, start=start, end=end)
    assert "Client Dinners" not in by_category_removed, (
        "a tombstoned transaction must not contribute to spending totals"
    )

    # A later sync replay (retried page, or Plaid re-adds a corrected
    # version of the same plaid_transaction_id) resurrects the row via the
    # ordinary `added` code path, `upsert`, which unconditionally clears
    # removed_at.
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-tombstone-resurrect",
            account_id=account_id,
            amount=Decimal("42.10"),
            date=datetime.date(2026, 1, 5),
            name="MOONLIGHT DINER",
            merchant_name="Moonlight Diner",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
    )
    session.commit()

    by_category_after = get_spending_by_category(session, start=start, end=end)

    # The stale override — created before the removal, never reviewed or
    # re-confirmed by the user — silently re-applies the instant the row
    # is resurrected. There is no signal anywhere that this happened.
    assert by_category_after.get("Client Dinners") == Decimal("42.10"), (
        "documents current behavior: a stale category override survives "
        "tombstoning and silently reattaches to a resurrected transaction "
        "with no audit trail of the reattachment"
    )


# ---------------------------------------------------------------------------
# 5. Month-boundary probes — tried to break, held.
# ---------------------------------------------------------------------------


def test_last_day_of_month_transaction_lands_in_the_correct_month(item_account) -> None:
    """Attack: a transaction dated exactly on the last calendar day of a
    month (including December -> January year rollover, and a 28-day
    February) must not leak into the following month's totals via an
    off-by-one in the half-open range."""
    session, account_id = item_account

    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-jan31",
            account_id=account_id,
            amount=Decimal("10.00"),
            date=datetime.date(2026, 1, 31),
            name="LAST DAY OF JANUARY",
            plaid_category=["Shops"],
        ),
    )
    upsert(
        session,
        TransactionFields(
            plaid_transaction_id="adv-feb1",
            account_id=account_id,
            amount=Decimal("20.00"),
            date=datetime.date(2026, 2, 1),
            name="FIRST DAY OF FEBRUARY",
            plaid_category=["Shops"],
        ),
    )
    session.commit()

    jan_start, jan_end = month_bounds(2026, 1)
    feb_start, feb_end = month_bounds(2026, 2)

    jan_totals = get_spending_by_category(session, start=jan_start, end=jan_end)
    feb_totals = get_spending_by_category(session, start=feb_start, end=feb_end)

    # Held: half-open [start, end) correctly places each boundary day.
    assert jan_totals.get("Shops") == Decimal("10.00")
    assert feb_totals.get("Shops") == Decimal("20.00")


def test_december_to_january_year_rollover_recurring_window(item_account) -> None:
    """Attack: `find_recurring_transactions`' lookback window walks
    backward across a year boundary (`start_month - 1` when month is
    January) — verify it doesn't skip or duplicate December of the prior
    year."""
    session, account_id = item_account

    for year, month in ((2025, 11), (2025, 12), (2026, 1)):
        upsert(
            session,
            TransactionFields(
                plaid_transaction_id=f"adv-rollover-{year}-{month:02d}",
                account_id=account_id,
                amount=Decimal("9.99"),
                date=datetime.date(year, month, 20),
                name="YEAR ROLLOVER SUB",
                merchant_name="Year Rollover Sub",
                plaid_category=["Service", "Subscription"],
            ),
        )
    session.commit()

    recurring = find_recurring_transactions(session, as_of=datetime.date(2026, 1, 25))
    merchants = {r.merchant: r for r in recurring}

    # Held: the 3-month lookback window correctly reaches back into
    # November/December of the prior year.
    assert "Year Rollover Sub" in merchants
    assert merchants["Year Rollover Sub"].occurrences == 3
