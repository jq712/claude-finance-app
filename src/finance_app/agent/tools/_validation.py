"""Shared argument validation for tool handlers.

Every handler in `read.py`/`write.py` runs its arguments through these
helpers before touching the database. This is the layer that stops
malformed or scope-widening tool calls — an out-of-range date, a
1900-2100 "history of everything" period, a negative budget amount —
before a query ever runs (see `.claude/agents/qa-adversarial.md`, "Tool
arguments that attempt to widen scope").
"""

import datetime
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from finance_app.agent.tools.errors import ToolInputError

# A period longer than this is rejected outright rather than silently
# truncated — a silent truncation could make the model report a total for
# a shorter window than it told the user it used (handoff §8.2: "report
# date ranges used").
MAX_PERIOD_DAYS = 3660  # ~10 years


def require_date(args: Mapping[str, Any], key: str) -> datetime.date:
    raw = args.get(key)
    if not isinstance(raw, str):
        raise ToolInputError(f"{key!r} is required and must be a YYYY-MM-DD date string")
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError as exc:
        raise ToolInputError(f"{key!r} must be a YYYY-MM-DD date, got {raw!r}") from exc


def require_period(
    args: Mapping[str, Any],
    *,
    start_key: str = "start",
    end_key: str = "end",
    max_days: int = MAX_PERIOD_DAYS,
) -> tuple[datetime.date, datetime.date]:
    start = require_date(args, start_key)
    end = require_date(args, end_key)
    if end <= start:
        raise ToolInputError(f"{end_key!r} must be after {start_key!r}")
    if (end - start).days > max_days:
        raise ToolInputError(f"the period between {start_key!r} and {end_key!r} is too wide")
    return start, end


def require_str(args: Mapping[str, Any], key: str, *, max_length: int = 500) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ToolInputError(f"{key!r} is required and must be a non-empty string")
    if len(raw) > max_length:
        raise ToolInputError(f"{key!r} must be at most {max_length} characters")
    return raw.strip()


def optional_str(args: Mapping[str, Any], key: str, *, max_length: int = 500) -> str | None:
    if args.get(key) is None:
        return None
    return require_str(args, key, max_length=max_length)


def require_int(
    args: Mapping[str, Any],
    key: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    default: int | None = None,
) -> int:
    raw = args.get(key, default)
    if raw is None:
        raise ToolInputError(f"{key!r} is required")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolInputError(f"{key!r} must be an integer")
    if minimum is not None and raw < minimum:
        raise ToolInputError(f"{key!r} must be >= {minimum}")
    if maximum is not None and raw > maximum:
        raise ToolInputError(f"{key!r} must be <= {maximum}")
    return raw


def require_transaction_id(args: Mapping[str, Any], key: str = "transaction_id") -> int:
    return require_int(args, key, minimum=1)


def require_amount(args: Mapping[str, Any], key: str) -> Decimal:
    raw = args.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ToolInputError(f"{key!r} must be a number")
    try:
        amount = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ToolInputError(f"{key!r} must be a valid decimal amount") from exc
    if amount <= 0:
        raise ToolInputError(f"{key!r} must be positive")
    if amount > Decimal("1000000"):
        raise ToolInputError(f"{key!r} is unreasonably large")
    return amount


def money(amount: Decimal) -> str:
    """Render a `Decimal` for a tool result. Always a string — JSON has no
    exact decimal type, and a bare float re-entering the model's context
    is exactly the binary-rounding risk CLAUDE.md's money rule exists to
    avoid."""
    return f"{amount:.2f}"
