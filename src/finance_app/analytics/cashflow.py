"""Deterministic cash flow: income, spending, and their difference for a
period. Reuses `analytics.income`/`analytics.spending` rather than
recomputing totals, so cash flow can never disagree with the standalone
income/spending numbers the CLI or agent reports elsewhere."""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from finance_app.analytics.income import get_income_summary
from finance_app.analytics.spending import get_spending_summary


@dataclass(frozen=True, slots=True)
class CashflowResult:
    period: tuple[datetime.date, datetime.date]
    income: Decimal
    spending: Decimal

    @property
    def net(self) -> Decimal:
        return self.income - self.spending

    @property
    def savings_rate(self) -> Decimal | None:
        """Net as a fraction of income. `None` when income is zero — a
        savings rate against zero income is undefined, not zero."""
        if self.income == 0:
            return None
        return self.net / self.income


def calculate_cashflow(
    session: Session, *, start: datetime.date, end: datetime.date
) -> CashflowResult:
    return CashflowResult(
        period=(start, end),
        income=get_income_summary(session, start=start, end=end),
        spending=get_spending_summary(session, start=start, end=end),
    )
