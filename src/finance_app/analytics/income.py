"""Deterministic income totals. Income rows are Plaid credits (negative
amount, per Plaid's sign convention) classified as income by
`analytics.categories.classify` — payroll and similar deposits, not every
negative amount (a same-category refund is spending, not income; see
`analytics/spending.py`)."""

import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from finance_app.analytics._effective import fetch_effective_transactions


def get_income_summary(session: Session, *, start: datetime.date, end: datetime.date) -> Decimal:
    """Total income in `[start, end)`, as a positive amount. Income rows
    are negative (credits); this returns their magnitude."""
    txns = fetch_effective_transactions(session, start=start, end=end)
    return -sum((t.amount for t in txns if t.is_income), Decimal("0"))
