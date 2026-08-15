"""Synthetic Plaid-shaped fixtures — no real financial data, ever.

Shapes mirror what `/transactions/sync` actually returns closely enough to
exercise ingestion, analytics, and eval code, without needing Plaid
Sandbox network access for most tests. Amounts follow Plaid's sign
convention: positive is money leaving the account, negative is a credit.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SyntheticItem:
    plaid_item_id: str = "synthetic-item-1"
    institution_id: str = "ins_synthetic"
    institution_name: str = "Synthetic Bank"


@dataclass(frozen=True, slots=True)
class SyntheticAccount:
    plaid_account_id: str = "synthetic-account-checking"
    name: str = "Synthetic Checking"
    type: str = "depository"
    subtype: str = "checking"
    mask: str = "1234"
    iso_currency_code: str = "USD"


@dataclass(frozen=True, slots=True)
class SyntheticTransaction:
    plaid_transaction_id: str
    amount: Decimal
    date: datetime.date
    name: str
    merchant_name: str | None = None
    pending: bool = False
    payment_channel: str = "other"
    plaid_category: list[str] = field(default_factory=list)
    authorized_date: datetime.date | None = None


def golden_month(month_start: datetime.date) -> list[SyntheticTransaction]:
    """One representative month covering handoff §24's golden-dataset
    categories: paycheck income, rent, restaurants, groceries, a refund, an
    internal transfer, ATM cash, and a subscription."""

    def day(n: int) -> datetime.date:
        return month_start + datetime.timedelta(days=n - 1)

    prefix = month_start.isoformat()
    return [
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-paycheck",
            amount=Decimal("-3200.00"),
            date=day(1),
            name="ACME CORP PAYROLL",
            merchant_name="Acme Corp",
            payment_channel="other",
            plaid_category=["Payroll"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-rent",
            amount=Decimal("1500.00"),
            date=day(1),
            name="RIVERSIDE PROPERTY MGMT",
            merchant_name="Riverside Property Management",
            payment_channel="other",
            plaid_category=["Rent"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-groceries-1",
            amount=Decimal("87.32"),
            date=day(3),
            name="TRADER JOE'S",
            merchant_name="Trader Joe's",
            payment_channel="in store",
            plaid_category=["Food and Drink", "Groceries"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-restaurant-1",
            amount=Decimal("42.10"),
            date=day(5),
            name="MOONLIGHT DINER",
            merchant_name="Moonlight Diner",
            payment_channel="in store",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-restaurant-refund",
            amount=Decimal("-15.00"),
            date=day(6),
            name="MOONLIGHT DINER REFUND",
            merchant_name="Moonlight Diner",
            payment_channel="in store",
            plaid_category=["Food and Drink", "Restaurants"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-transfer-savings",
            amount=Decimal("500.00"),
            date=day(7),
            name="TRANSFER TO SAVINGS",
            payment_channel="other",
            plaid_category=["Transfer"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-atm",
            amount=Decimal("100.00"),
            date=day(9),
            name="ATM WITHDRAWAL",
            payment_channel="other",
            plaid_category=["Cash Advance"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-subscription",
            amount=Decimal("15.99"),
            date=day(10),
            name="STREAMBOX SUBSCRIPTION",
            merchant_name="Streambox",
            payment_channel="online",
            plaid_category=["Service", "Subscription"],
        ),
        SyntheticTransaction(
            plaid_transaction_id=f"{prefix}-pending-coffee",
            amount=Decimal("6.25"),
            date=day(12),
            authorized_date=day(12),
            name="CORNER CAFE",
            merchant_name="Corner Cafe",
            pending=True,
            payment_channel="in store",
            plaid_category=["Food and Drink", "Coffee Shop"],
        ),
    ]
