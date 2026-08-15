"""Recurring-transaction detection: merchants billing roughly the same
amount in at least `min_occurrences` distinct calendar months within the
lookback window. Heuristic, not a Plaid-verified fact — callers (CLI, and
eventually the agent) must present this as "looks recurring", never as a
certainty. See handoff §8.2: "avoid pretending that transaction
categorization is certainty when it is heuristic"."""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from finance_app.analytics._effective import fetch_effective_transactions
from finance_app.analytics.periods import month_bounds

_DEFAULT_AMOUNT_TOLERANCE = Decimal("0.05")  # 5%


@dataclass(frozen=True, slots=True)
class RecurringMerchant:
    merchant: str
    occurrences: int
    average_amount: Decimal
    last_date: datetime.date


def find_recurring_transactions(
    session: Session,
    *,
    as_of: datetime.date,
    lookback_months: int = 3,
    min_occurrences: int = 2,
    amount_tolerance: Decimal = _DEFAULT_AMOUNT_TOLERANCE,
) -> list[RecurringMerchant]:
    """Merchants that billed a similar amount in at least `min_occurrences`
    distinct calendar months out of the `lookback_months` ending with the
    month containing `as_of`. Transfers and income are excluded — a
    recurring paycheck isn't a "recurring charge" in the sense this
    function means."""
    start_year, start_month = as_of.year, as_of.month
    for _ in range(lookback_months - 1):
        start_year, start_month = (
            (start_year - 1, 12) if start_month == 1 else (start_year, start_month - 1)
        )
    window_start, _ = month_bounds(start_year, start_month)
    _, window_end = month_bounds(as_of.year, as_of.month)

    txns = fetch_effective_transactions(session, start=window_start, end=window_end)

    # Plaid's merchant-name enrichment is probabilistic and can be present
    # for a transaction one month and absent the next, for the same real
    # merchant and the same raw `name` description. Grouping strictly by
    # `merchant_name or name` would then split one recurring series into
    # two, each below the occurrence threshold. Resolve every occurrence of
    # a given raw `name` to the same key by preferring any `merchant_name`
    # ever seen for that `name` within the window.
    name_to_merchant: dict[str, str] = {}
    for t in txns:
        if t.merchant_name and t.name not in name_to_merchant:
            name_to_merchant[t.name] = t.merchant_name

    by_merchant: dict[str, list[tuple[datetime.date, Decimal]]] = {}
    for t in txns:
        if t.is_transfer or t.is_income:
            continue
        label = t.merchant_name or name_to_merchant.get(t.name) or t.name
        by_merchant.setdefault(label, []).append((t.date, t.amount))

    results: list[RecurringMerchant] = []
    for merchant, entries in by_merchant.items():
        distinct_months = {(d.year, d.month) for d, _amount in entries}
        if len(distinct_months) < min_occurrences:
            continue

        amounts = [amount for _date, amount in entries]
        median_amount = sorted(amounts)[len(amounts) // 2]
        if median_amount == 0:
            continue
        tolerance = abs(median_amount) * amount_tolerance
        if any(abs(amount - median_amount) > tolerance for amount in amounts):
            continue

        results.append(
            RecurringMerchant(
                merchant=merchant,
                occurrences=len(entries),
                average_amount=sum(amounts, Decimal("0")) / len(amounts),
                last_date=max(d for d, _amount in entries),
            )
        )

    return sorted(results, key=lambda r: r.merchant)
