"""Deterministic spending totals. Every number here traces to a plain
Decimal sum over `plaid.transactions` rows — no model, LLM, or float ever
computes a total (CLAUDE.md: "the model explains numbers, it never
produces them").

A same-category refund nets against its original purchase because both
are ordinary spending rows summed together — see
`EffectiveTransaction.is_spending` in `analytics/_effective.py`. Pending
transactions are included: they represent a real, bank-reported hold on
the account, and a spending total that ignored them would understate
current-period spend until settlement, sometimes by days.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from finance_app.analytics._effective import fetch_effective_transactions


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    period_a: tuple[datetime.date, datetime.date]
    period_b: tuple[datetime.date, datetime.date]
    amount_a: Decimal
    amount_b: Decimal

    @property
    def change(self) -> Decimal:
        return self.amount_b - self.amount_a

    @property
    def change_pct(self) -> Decimal | None:
        """`None` when period A had zero spend — a percentage change from
        zero is undefined, not zero."""
        if self.amount_a == 0:
            return None
        return (self.change / self.amount_a) * Decimal(100)


def get_spending_summary(session: Session, *, start: datetime.date, end: datetime.date) -> Decimal:
    """Net spending across every category in `[start, end)`. Excludes
    transfers and income; includes refunds netted against their category."""
    txns = fetch_effective_transactions(session, start=start, end=end)
    return sum((t.amount for t in txns if t.is_spending), Decimal("0"))


def get_spending_by_category(
    session: Session, *, start: datetime.date, end: datetime.date
) -> dict[str, Decimal]:
    """Net spending per effective category in `[start, end)`, sorted by
    amount descending. Categories that net to exactly zero (e.g. a
    purchase fully refunded within the period) are omitted."""
    txns = fetch_effective_transactions(session, start=start, end=end)
    totals: dict[str, Decimal] = {}
    for t in txns:
        if not t.is_spending:
            continue
        totals[t.effective_category] = totals.get(t.effective_category, Decimal("0")) + t.amount
    nonzero = {category: amount for category, amount in totals.items() if amount != 0}
    return dict(sorted(nonzero.items(), key=lambda item: item[1], reverse=True))


def compare_periods(
    session: Session,
    *,
    period_a: tuple[datetime.date, datetime.date],
    period_b: tuple[datetime.date, datetime.date],
    category: str | None = None,
) -> PeriodComparison:
    """Spending in two periods, optionally scoped to one effective
    category. `category` matching is case-insensitive but otherwise
    exact — it must match a label `get_spending_by_category` would have
    produced. This does not reconcile the *same* real-world category
    reported under two genuinely different labels (e.g. Plaid's legacy
    taxonomy vs. `personal_finance_category` producing different strings
    for what a human would call the same category) — see
    docs/analytics.md's Known limitations."""
    target = category.casefold() if category is not None else None

    def _amount(period: tuple[datetime.date, datetime.date]) -> Decimal:
        start, end = period
        txns = fetch_effective_transactions(session, start=start, end=end)
        return sum(
            (
                t.amount
                for t in txns
                if t.is_spending and (target is None or t.effective_category.casefold() == target)
            ),
            Decimal("0"),
        )

    return PeriodComparison(
        period_a=period_a,
        period_b=period_b,
        amount_a=_amount(period_a),
        amount_b=_amount(period_b),
    )
