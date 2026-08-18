"""Golden financial dataset for Milestone 6 agent evals (handoff §24).

Synthetic transactions across May and June 2026 covering: paycheck income,
rent, restaurants, groceries, a same-category refund, an internal
transfer, ATM cash, a recurring subscription, a pending transaction, a
Plaid-style modified record, a removed record, merchant-name ambiguity,
a budget, and a user category override. Seeded through the `finance_app`
role (the only role allowed to write `plaid.*`), mirroring
`tests/integration/test_agent_tools.py`'s fixture shape.

Every constant below was computed by hand from the raw rows in
`seed_golden_dataset`, independently of the analytics code under test —
see `docs/financial-agent.md`'s eval framework section for the worked
arithmetic. If you change a row's amount/date/category, the constant it
feeds must be recomputed by hand too, not by running the code and pasting
the result back.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.db.models.plaid import Account, Item, Transaction
from finance_app.db.repositories import budgets as budgets_repo
from finance_app.db.repositories import user_annotations

MAY_START = datetime.date(2026, 5, 1)
MAY_END = datetime.date(2026, 6, 1)
JUNE_START = datetime.date(2026, 6, 1)
JUNE_END = datetime.date(2026, 7, 1)
AS_OF = datetime.date(2026, 6, 30)

# -- Hand-computed expected values --
JUNE_TOTAL_SPENDING = Decimal("2470.24")
JUNE_TOTAL_INCOME = Decimal("5000.00")
JUNE_NET_CASHFLOW = Decimal("2529.76")
JUNE_SAVINGS_RATE = "0.506"  # calculate_cashflow's `f"{rate:.3f}"` formatting

MAY_RESTAURANTS = Decimal("40.00")
JUNE_RESTAURANTS = Decimal("60.00")  # 45.00 + 30.00 - 15.00 refund, netted
RESTAURANTS_CHANGE_PCT = "50.0"

JUNE_GROCERIES = Decimal("120.00")  # the second trip is moved to "Household" by the override
JUNE_HOUSEHOLD = Decimal("95.00")

RESTAURANTS_BUDGET_MONTHLY = Decimal("50.00")

RECURRING_MERCHANT = "Netflix.com"
RECURRING_OCCURRENCES = 2
RECURRING_AVERAGE_AMOUNT = "15.99"

TARGET_AMOUNT = Decimal("60.00")


@dataclass(frozen=True, slots=True)
class GoldenDataset:
    item_id: int
    account_id: int
    target_transaction_id: int
    targetron_transaction_id: int
    removed_transaction_id: int
    grocery_override_transaction_id: int


def seed_golden_dataset(session: Session) -> GoldenDataset:
    """Insert the golden dataset via `session` (must be bound to the
    `finance_app` role — the only role allowed to write `plaid.*`).
    Caller owns commit/rollback and must call `cleanup_golden_dataset`
    when done."""
    item = Item(plaid_item_id="eval-golden-item", institution_name="Golden Test Bank")
    session.add(item)
    session.flush()

    account = Account(
        item_id=item.id,
        plaid_account_id="eval-golden-checking",
        name="Golden Checking",
        type="depository",
    )
    session.add(account)
    session.flush()

    def txn(
        plaid_id: str,
        amount: str,
        date: datetime.date,
        name: str,
        category: list[str],
        *,
        merchant_name: str | None = None,
        pending: bool = False,
        removed_at: datetime.datetime | None = None,
    ) -> Transaction:
        row = Transaction(
            account_id=account.id,
            plaid_transaction_id=plaid_id,
            amount=Decimal(amount),
            date=date,
            name=name,
            merchant_name=merchant_name,
            pending=pending,
            plaid_category=category,
            removed_at=removed_at,
        )
        session.add(row)
        session.flush()
        return row

    # -- May 2026: baseline for compare_periods and recurring detection --
    txn(
        "eval-may-payroll-1",
        "-2500.00",
        datetime.date(2026, 5, 1),
        "ACME CORP PAYROLL",
        ["Payroll"],
    )
    txn(
        "eval-may-payroll-2",
        "-2500.00",
        datetime.date(2026, 5, 15),
        "ACME CORP PAYROLL",
        ["Payroll"],
    )
    txn(
        "eval-may-rent", "1800.00", datetime.date(2026, 5, 1), "RENT PAYMENT LANDLORD LLC", ["Rent"]
    )
    txn(
        "eval-may-netflix",
        "15.99",
        datetime.date(2026, 5, 3),
        "NETFLIX.COM",
        ["Service", "Subscription"],
        merchant_name="Netflix.com",
    )
    txn(
        "eval-may-burger",
        "40.00",
        datetime.date(2026, 5, 10),
        "TASTY BURGER",
        ["Food and Drink", "Restaurants"],
    )

    # -- June 2026 --
    txn(
        "eval-jun-payroll-1",
        "-2500.00",
        datetime.date(2026, 6, 1),
        "ACME CORP PAYROLL",
        ["Payroll"],
    )
    txn(
        "eval-jun-payroll-2",
        "-2500.00",
        datetime.date(2026, 6, 15),
        "ACME CORP PAYROLL",
        ["Payroll"],
    )
    txn(
        "eval-jun-rent", "1800.00", datetime.date(2026, 6, 1), "RENT PAYMENT LANDLORD LLC", ["Rent"]
    )
    txn(
        "eval-jun-netflix",
        "15.99",
        datetime.date(2026, 6, 3),
        "NETFLIX.COM",
        ["Service", "Subscription"],
        merchant_name="Netflix.com",
    )
    txn(
        "eval-jun-coffee",
        "12.50",
        datetime.date(2026, 6, 5),
        "COFFEE SHOP",
        ["Food and Drink", "Coffee Shop"],
    )
    txn(
        "eval-jun-starbucks-pending",
        "6.75",
        datetime.date(2026, 6, 25),
        "STARBUCKS #1234",
        ["Food and Drink", "Coffee Shop"],
        merchant_name="Starbucks",
        pending=True,
    )
    txn(
        "eval-jun-burger-1",
        "45.00",
        datetime.date(2026, 6, 10),
        "TASTY BURGER",
        ["Food and Drink", "Restaurants"],
    )
    txn(
        "eval-jun-burger-2",
        "30.00",
        datetime.date(2026, 6, 20),
        "TASTY BURGER",
        ["Food and Drink", "Restaurants"],
    )
    txn(
        "eval-jun-burger-refund",
        "-15.00",
        datetime.date(2026, 6, 22),
        "TASTY BURGER REFUND",
        ["Food and Drink", "Restaurants"],
    )
    grocery_1 = txn(
        "eval-jun-groceries-1",
        "120.00",
        datetime.date(2026, 6, 8),
        "WHOLE FOODS MARKET",
        ["Shops", "Groceries"],
    )
    grocery_2 = txn(
        "eval-jun-groceries-2",
        "95.00",
        datetime.date(2026, 6, 18),
        "WHOLE FOODS MARKET",
        ["Shops", "Groceries"],
    )
    txn(
        "eval-jun-transfer",
        "500.00",
        datetime.date(2026, 6, 12),
        "TRANSFER TO SAVINGS",
        ["Transfer"],
    )

    # A Plaid "modified" event correcting a previously-synced amount, without
    # duplicating Milestone 2's own sync-cursor tests.
    atm = txn(
        "eval-jun-atm", "90.00", datetime.date(2026, 6, 14), "ATM WITHDRAWAL", ["Cash", "ATM"]
    )
    atm.amount = Decimal("100.00")
    session.flush()

    target = txn(
        "eval-jun-target",
        "60.00",
        datetime.date(2026, 6, 9),
        "TARGET",
        ["Shops", "General Merchandise"],
        merchant_name="Target",
    )
    targetron = txn(
        "eval-jun-targetron",
        "200.00",
        datetime.date(2026, 6, 11),
        "TARGETRON ELECTRONICS",
        ["Shops", "Electronics"],
        merchant_name="Targetron Electronics",
    )
    removed = txn(
        "eval-jun-removed-duplicate",
        "25.00",
        datetime.date(2026, 6, 7),
        "DUPLICATE CHARGE ERROR",
        ["Shops", "General"],
        removed_at=datetime.datetime(2026, 6, 8, tzinfo=datetime.UTC),
    )

    budgets_repo.create(session, category="Restaurants", monthly_amount=RESTAURANTS_BUDGET_MONTHLY)
    user_annotations.set_category_override(
        session, transaction_id=grocery_2.id, category="Household", source="fixture"
    )
    session.flush()
    del grocery_1

    return GoldenDataset(
        item_id=item.id,
        account_id=account.id,
        target_transaction_id=target.id,
        targetron_transaction_id=targetron.id,
        removed_transaction_id=removed.id,
        grocery_override_transaction_id=grocery_2.id,
    )


def cleanup_golden_dataset(session: Session, dataset: GoldenDataset) -> None:
    """Delete the seeded rows plus anything a tool call under test may
    have added on top of them (overrides, notes, tags, budgets) — mirrors
    `tests/integration/test_agent_tools.py`'s teardown."""
    session.execute(
        text(
            'DELETE FROM "user".transaction_category_overrides WHERE transaction_id IN '
            "(SELECT id FROM plaid.transactions WHERE account_id = :a)"
        ),
        {"a": dataset.account_id},
    )
    session.execute(
        text(
            'DELETE FROM "user".transaction_notes WHERE transaction_id IN '
            "(SELECT id FROM plaid.transactions WHERE account_id = :a)"
        ),
        {"a": dataset.account_id},
    )
    session.execute(
        text(
            'DELETE FROM "user".transaction_tags WHERE transaction_id IN '
            "(SELECT id FROM plaid.transactions WHERE account_id = :a)"
        ),
        {"a": dataset.account_id},
    )
    session.execute(text("DELETE FROM finance.budgets WHERE category = 'Restaurants'"))
    session.execute(
        text("DELETE FROM plaid.transactions WHERE account_id = :a"), {"a": dataset.account_id}
    )
    session.execute(text("DELETE FROM plaid.accounts WHERE id = :a"), {"a": dataset.account_id})
    session.execute(text("DELETE FROM plaid.items WHERE id = :i"), {"i": dataset.item_id})
    session.commit()
