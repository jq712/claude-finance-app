"""Shared query: every non-removed transaction in a date range, joined
with its override (if any) and classified via `analytics.categories`.
Every analytics module in this package builds on this single fetch, so
the effective-category/transfer/income rules apply identically everywhere
— a category label or transfer decision can't drift between spending.py
and cashflow.py because there's only one place either is computed.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.analytics.categories import TransactionClassification, classify
from finance_app.db.models.plaid import Transaction
from finance_app.db.models.user import TransactionCategoryOverride


@dataclass(frozen=True, slots=True)
class EffectiveTransaction:
    transaction_id: int
    date: datetime.date
    amount: Decimal
    name: str
    merchant_name: str | None
    pending: bool
    classification: TransactionClassification

    @property
    def effective_category(self) -> str:
        return self.classification.effective_category

    @property
    def is_transfer(self) -> bool:
        return self.classification.is_transfer

    @property
    def is_income(self) -> bool:
        return self.classification.is_income

    @property
    def is_spending(self) -> bool:
        """Counts toward spending totals: not a transfer, not income. Both
        an ordinary purchase (positive amount) and a same-category refund
        (negative amount) are spending rows — netting a refund against its
        original purchase within a category is the intended behavior, not
        a special case; see `analytics/spending.py`."""
        return not self.is_transfer and not self.is_income


def fetch_effective_transactions(
    session: Session, *, start: datetime.date, end: datetime.date
) -> list[EffectiveTransaction]:
    """Every non-removed transaction with `date` in `[start, end)`,
    ordered by date. `end` is exclusive — see `analytics/periods.py`."""
    rows = session.execute(
        select(
            Transaction.id,
            Transaction.date,
            Transaction.amount,
            Transaction.name,
            Transaction.merchant_name,
            Transaction.pending,
            Transaction.plaid_category,
            Transaction.personal_finance_category,
            TransactionCategoryOverride.category,
        )
        .outerjoin(
            TransactionCategoryOverride,
            TransactionCategoryOverride.transaction_id == Transaction.id,
        )
        .where(
            Transaction.removed_at.is_(None),
            Transaction.date >= start,
            Transaction.date < end,
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()

    return [
        EffectiveTransaction(
            transaction_id=row.id,
            date=row.date,
            amount=row.amount,
            name=row.name,
            merchant_name=row.merchant_name,
            pending=row.pending,
            classification=classify(
                override_category=row.category,
                personal_finance_category=row.personal_finance_category,
                plaid_category=row.plaid_category,
            ),
        )
        for row in rows
    ]
