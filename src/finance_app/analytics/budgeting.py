"""Deterministic budget variance: each active `finance.budgets` row
against actual spending in the same effective category for a period.
Budgets are user/agent-owned interpretation (handoff §7), never written by
this module — it only reads them and compares against the spending
totals `analytics.spending` already computes."""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.analytics.spending import get_spending_by_category
from finance_app.db.models.finance import Budget


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    category: str
    monthly_amount: Decimal
    actual_spent: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.monthly_amount - self.actual_spent

    @property
    def over_budget(self) -> bool:
        return self.actual_spent > self.monthly_amount


def get_budget_status(
    session: Session, *, start: datetime.date, end: datetime.date
) -> list[BudgetStatus]:
    """One `BudgetStatus` per active budget, actual spend computed over
    `[start, end)` and matched against that budget's category label
    case-insensitively — a budget category is free text a human typed, and
    "restaurants" must match a derived "Restaurants" label. This does not
    reconcile a budget category against a *different* label for the same
    real-world category (e.g. Plaid's legacy taxonomy vs.
    `personal_finance_category` producing different strings for what a
    human would call the same category) — see docs/analytics.md's Known
    limitations. A category with an active budget but zero matching spend
    still appears, with `actual_spent == 0` — the budget exists whether or
    not it was used."""
    budgets = (
        session.execute(select(Budget).where(Budget.active.is_(True)).order_by(Budget.category))
        .scalars()
        .all()
    )
    spending_by_category = get_spending_by_category(session, start=start, end=end)
    spending_casefolded = {
        category.casefold(): amount for category, amount in spending_by_category.items()
    }
    return [
        BudgetStatus(
            category=budget.category,
            monthly_amount=budget.monthly_amount,
            actual_spent=spending_casefolded.get(budget.category.casefold(), Decimal("0")),
        )
        for budget in budgets
    ]
