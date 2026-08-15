"""Calendar-month boundaries as half-open `[start, end)` date ranges.

Half-open so a period comparison or monthly sum never double-counts or
drops the transaction dated exactly on a month boundary (handoff §30:
"monthly boundaries, timezones"). `plaid.transactions.date` is a plain
`Date` — Plaid reports the transaction's local calendar date, not an
instant — so no timezone conversion belongs here or anywhere in this
package.
"""

import datetime


def month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """`[start, end)` for the given calendar month."""
    start = datetime.date(year, month, 1)
    end = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    return start, end


def month_of(day: datetime.date) -> tuple[datetime.date, datetime.date]:
    """`[start, end)` for the calendar month containing `day`."""
    return month_bounds(day.year, day.month)


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)
