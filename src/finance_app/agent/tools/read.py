"""Read-only semantic tools. Every number returned here traces to a
deterministic analytics function — no tool in this module ever sums,
averages, or compares amounts itself (CLAUDE.md: "the model explains
numbers; it never produces them")."""

import datetime
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from finance_app.agent.tools._validation import (
    money,
    optional_str,
    require_int,
    require_period,
    require_str,
)
from finance_app.agent.tools.base import ToolDefinition
from finance_app.agent.tools.errors import ToolInputError
from finance_app.analytics.budgeting import get_budget_status as _get_budget_status
from finance_app.analytics.cashflow import calculate_cashflow as _calculate_cashflow
from finance_app.analytics.income import get_income_summary as _get_income_summary
from finance_app.analytics.recurring import find_recurring_transactions as _find_recurring
from finance_app.analytics.spending import compare_periods as _compare_periods
from finance_app.analytics.spending import get_spending_by_category as _get_spending_by_category
from finance_app.analytics.spending import get_spending_summary as _get_spending_summary
from finance_app.analytics.transactions import TransactionRecord
from finance_app.analytics.transactions import list_transactions as _list_transactions
from finance_app.analytics.transactions import search_transactions as _search_transactions

_PERIOD_SCHEMA = {
    "start": {"type": "string", "description": "Period start date, inclusive, YYYY-MM-DD."},
    "end": {"type": "string", "description": "Period end date, exclusive, YYYY-MM-DD."},
}


def _transaction_dict(record: TransactionRecord) -> dict[str, Any]:
    return {
        "transaction_id": record.transaction_id,
        "date": record.date.isoformat(),
        "name": record.name,
        "merchant_name": record.merchant_name,
        "amount": money(record.amount),
        "effective_category": record.effective_category,
        "pending": record.pending,
    }


def get_transactions(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    start, end = require_period(args)
    limit = require_int(args, "limit", minimum=1, maximum=200, default=50)
    records = _list_transactions(session, start=start, end=end, limit=limit)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "count": len(records),
        "transactions": [_transaction_dict(r) for r in records],
    }


def search_transactions(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    query = require_str(args, "query", max_length=200)
    start, end = require_period(args)
    limit = require_int(args, "limit", minimum=1, maximum=200, default=50)
    records = _search_transactions(session, query=query, start=start, end=end, limit=limit)
    return {
        "query": query,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "count": len(records),
        "transactions": [_transaction_dict(r) for r in records],
    }


def get_spending_summary(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    start, end = require_period(args)
    total = _get_spending_summary(session, start=start, end=end)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "total_spent": money(total),
    }


def get_spending_by_category(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    start, end = require_period(args)
    by_category = _get_spending_by_category(session, start=start, end=end)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "categories": [
            {"category": category, "amount": money(amount)}
            for category, amount in by_category.items()
        ],
    }


def get_category_summary(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    """Same deterministic per-category totals as `get_spending_by_category`,
    shaped with a total and category count for a "what am I spending on"
    style question — presentation only, no new arithmetic."""
    start, end = require_period(args)
    by_category = _get_spending_by_category(session, start=start, end=end)
    total = sum(by_category.values(), start=by_category[next(iter(by_category))].__class__(0))
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "category_count": len(by_category),
        "total_spent": money(total),
        "categories": [
            {"category": category, "amount": money(amount)}
            for category, amount in by_category.items()
        ],
    }


def get_income_summary(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    start, end = require_period(args)
    total = _get_income_summary(session, start=start, end=end)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "total_income": money(total),
    }


def compare_periods(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    period_a = require_period(args, start_key="period_a_start", end_key="period_a_end")
    period_b = require_period(args, start_key="period_b_start", end_key="period_b_end")
    category = optional_str(args, "category", max_length=128)
    result = _compare_periods(session, period_a=period_a, period_b=period_b, category=category)
    return {
        "category": category,
        "period_a": {"start": period_a[0].isoformat(), "end": period_a[1].isoformat()},
        "period_b": {"start": period_b[0].isoformat(), "end": period_b[1].isoformat()},
        "amount_a": money(result.amount_a),
        "amount_b": money(result.amount_b),
        "change": money(result.change),
        "change_pct": None if result.change_pct is None else f"{result.change_pct:.1f}",
    }


def calculate_cashflow(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    start, end = require_period(args)
    result = _calculate_cashflow(session, start=start, end=end)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "income": money(result.income),
        "spending": money(result.spending),
        "net": money(result.net),
        "savings_rate": None if result.savings_rate is None else f"{result.savings_rate:.3f}",
    }


def get_budget_status(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    start, end = require_period(args)
    statuses = _get_budget_status(session, start=start, end=end)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "budgets": [
            {
                "category": s.category,
                "monthly_amount": money(s.monthly_amount),
                "actual_spent": money(s.actual_spent),
                "remaining": money(s.remaining),
                "over_budget": s.over_budget,
            }
            for s in statuses
        ],
    }


def find_recurring_transactions(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    as_of_raw = args.get("as_of")
    if as_of_raw is None:
        as_of = datetime.date.today()
    elif isinstance(as_of_raw, str):
        try:
            as_of = datetime.date.fromisoformat(as_of_raw)
        except ValueError as exc:
            raise ToolInputError(f"'as_of' must be a YYYY-MM-DD date, got {as_of_raw!r}") from exc
    else:
        raise ToolInputError("'as_of' must be a YYYY-MM-DD date string")
    lookback_months = require_int(args, "lookback_months", minimum=1, maximum=24, default=3)
    min_occurrences = require_int(args, "min_occurrences", minimum=2, maximum=24, default=2)

    results = _find_recurring(
        session,
        as_of=as_of,
        lookback_months=lookback_months,
        min_occurrences=min_occurrences,
    )
    return {
        "as_of": as_of.isoformat(),
        "lookback_months": lookback_months,
        "note": "Heuristic detection, not a Plaid-verified fact — present as 'looks recurring'.",
        "merchants": [
            {
                "merchant": r.merchant,
                "occurrences": r.occurrences,
                "average_amount": money(r.average_amount),
                "last_date": r.last_date.isoformat(),
            }
            for r in results
        ],
    }


READ_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_transactions",
        description="List transactions in a date range, newest first.",
        parameters={
            "type": "object",
            "properties": {
                **_PERIOD_SCHEMA,
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 200).",
                },
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=get_transactions,
    ),
    ToolDefinition(
        name="search_transactions",
        description=(
            "Search transactions by a case-insensitive substring of name or "
            "merchant, within a date range."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to match against name/merchant.",
                },
                **_PERIOD_SCHEMA,
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50, max 200).",
                },
            },
            "required": ["query", "start", "end"],
            "additionalProperties": False,
        },
        handler=search_transactions,
    ),
    ToolDefinition(
        name="get_spending_summary",
        description="Total net spending across all categories in a date range.",
        parameters={
            "type": "object",
            "properties": {**_PERIOD_SCHEMA},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=get_spending_summary,
    ),
    ToolDefinition(
        name="get_spending_by_category",
        description="Net spending per effective category in a date range, highest first.",
        parameters={
            "type": "object",
            "properties": {**_PERIOD_SCHEMA},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=get_spending_by_category,
    ),
    ToolDefinition(
        name="get_category_summary",
        description=(
            "Per-category spending totals for a date range, with the "
            "overall total and category count."
        ),
        parameters={
            "type": "object",
            "properties": {**_PERIOD_SCHEMA},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=get_category_summary,
    ),
    ToolDefinition(
        name="get_income_summary",
        description="Total income (payroll and similar credits) in a date range.",
        parameters={
            "type": "object",
            "properties": {**_PERIOD_SCHEMA},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=get_income_summary,
    ),
    ToolDefinition(
        name="compare_periods",
        description=(
            "Compare net spending between two date ranges, optionally "
            "scoped to one effective category."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period_a_start": {
                    "type": "string",
                    "description": "First period start, YYYY-MM-DD.",
                },
                "period_a_end": {"type": "string", "description": "First period end, YYYY-MM-DD."},
                "period_b_start": {
                    "type": "string",
                    "description": "Second period start, YYYY-MM-DD.",
                },
                "period_b_end": {"type": "string", "description": "Second period end, YYYY-MM-DD."},
                "category": {
                    "type": "string",
                    "description": "Optional effective category to scope to.",
                },
            },
            "required": ["period_a_start", "period_a_end", "period_b_start", "period_b_end"],
            "additionalProperties": False,
        },
        handler=compare_periods,
    ),
    ToolDefinition(
        name="calculate_cashflow",
        description="Income, spending, net cash flow, and savings rate for a date range.",
        parameters={
            "type": "object",
            "properties": {**_PERIOD_SCHEMA},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=calculate_cashflow,
    ),
    ToolDefinition(
        name="get_budget_status",
        description="Active budgets against actual spending in a date range.",
        parameters={
            "type": "object",
            "properties": {**_PERIOD_SCHEMA},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        handler=get_budget_status,
    ),
    ToolDefinition(
        name="find_recurring_transactions",
        description=(
            "Heuristically detect merchants billing a similar amount across multiple months. "
            "Not a certainty — present results as 'looks recurring'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "string",
                    "description": "Reference date, YYYY-MM-DD (default: today).",
                },
                "lookback_months": {
                    "type": "integer",
                    "description": "Months to look back (default 3, max 24).",
                },
                "min_occurrences": {
                    "type": "integer",
                    "description": (
                        "Minimum distinct months billed to count as recurring (default 2)."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=find_recurring_transactions,
    ),
]
