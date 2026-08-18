"""Deterministic writes to `finance.budgets` — modeled user constructs the
runtime agent's `create_budget`/`update_budget`/`archive_budget` tools
compose on top of the deterministic spending analytics (handoff §7).
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_app.db.models.finance import Budget


def get_by_category(session: Session, *, category: str) -> Budget | None:
    """Case-insensitive lookup — budget categories are free text a human
    typed, matching how `analytics.budgeting.get_budget_status` compares
    a budget's category against a derived spending label."""
    return session.execute(
        select(Budget).where(func.lower(Budget.category) == category.casefold())
    ).scalar_one_or_none()


def create(session: Session, *, category: str, monthly_amount: Decimal) -> Budget:
    budget = Budget(category=category, monthly_amount=monthly_amount, active=True)
    session.add(budget)
    session.flush()
    return budget


def update_amount(session: Session, *, budget: Budget, monthly_amount: Decimal) -> Budget:
    budget.monthly_amount = monthly_amount
    session.flush()
    return budget


def archive(session: Session, *, budget: Budget) -> Budget:
    budget.active = False
    session.flush()
    return budget
