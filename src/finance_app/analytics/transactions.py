"""Transaction listing/search built on the same effective-transaction view
as every other analytics module (see `analytics/_effective.py`) — the
CLI's `transactions recent`/`transactions search` commands see the exact
same effective category as `spending`/`budget`, with no separate query
path to drift out of sync.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from finance_app.analytics._effective import EffectiveTransaction, fetch_effective_transactions


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    transaction_id: int
    date: datetime.date
    name: str
    merchant_name: str | None
    amount: Decimal
    effective_category: str
    pending: bool


def _to_record(t: EffectiveTransaction) -> TransactionRecord:
    return TransactionRecord(
        transaction_id=t.transaction_id,
        date=t.date,
        name=t.name,
        merchant_name=t.merchant_name,
        amount=t.amount,
        effective_category=t.effective_category,
        pending=t.pending,
    )


def list_recent(
    session: Session, *, as_of: datetime.date, days: int = 30, limit: int = 50
) -> list[TransactionRecord]:
    """The most recent `limit` transactions dated within `days` of
    `as_of` (inclusive), newest first."""
    start = as_of - datetime.timedelta(days=days)
    end = as_of + datetime.timedelta(days=1)  # half-open range; include as_of itself
    txns = fetch_effective_transactions(session, start=start, end=end)
    ordered = sorted(txns, key=lambda t: (t.date, t.transaction_id), reverse=True)
    return [_to_record(t) for t in ordered[:limit]]


def list_transactions(
    session: Session, *, start: datetime.date, end: datetime.date, limit: int = 50
) -> list[TransactionRecord]:
    """Every transaction in `[start, end)`, newest first, capped at
    `limit`. Unlike `list_recent`, the caller supplies the range directly
    rather than a lookback window from today — this is what the agent's
    `get_transactions` tool needs for an arbitrary user-specified period."""
    txns = fetch_effective_transactions(session, start=start, end=end)
    ordered = sorted(txns, key=lambda t: (t.date, t.transaction_id), reverse=True)
    return [_to_record(t) for t in ordered[:limit]]


def search_transactions(
    session: Session,
    *,
    query: str,
    start: datetime.date,
    end: datetime.date,
    limit: int = 50,
) -> list[TransactionRecord]:
    """Transactions in `[start, end)` whose name or merchant name contains
    `query`, case-insensitively, newest first."""
    needle = query.casefold()
    txns = fetch_effective_transactions(session, start=start, end=end)
    matches = [
        t
        for t in txns
        if needle in t.name.casefold()
        or (t.merchant_name is not None and needle in t.merchant_name.casefold())
    ]
    ordered = sorted(matches, key=lambda t: (t.date, t.transaction_id), reverse=True)
    return [_to_record(t) for t in ordered[:limit]]
