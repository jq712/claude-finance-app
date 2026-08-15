# Deterministic analytics

How `finance_app.analytics.*` turns `plaid.transactions` rows into
spending, income, cash flow, budget, and recurring-charge numbers — and
why the non-obvious decisions below are correct, not accidental. Every
number here traces to a `Decimal` sum over database rows; no model, LLM,
or float ever produces one (CLAUDE.md).

## Effective category

`analytics/categories.py` is the single place that decides what a
transaction *is*. This is the handoff's core idea made concrete:

```text
Plaid fact + user/agent interpretation = effective financial view
```

- **No override**: the effective category is Plaid's `personal_finance_category.detailed`
  (or `.primary` if `detailed` is absent) when Plaid supplied it, else the
  most specific element of the legacy `plaid_category` list (`["Food and
  Drink", "Restaurants"]` → `"Restaurants"`, not `"Food and Drink"` —
  matching how the handoff's own examples name categories).
- **With an override** (`user.transaction_category_overrides`): the
  override's category string wins outright, and the transaction is no
  longer treated as a transfer or income by classification — see below.

## Transfers and income are excluded from spending, and an override wins

A transaction is a transfer or income based on Plaid's raw classification
(`personal_finance_category.primary` in `{TRANSFER_IN, TRANSFER_OUT}`, or
the legacy top-level `plaid_category[0]` being `"Transfer"`; income is the
equivalent for `Payroll`/`INCOME_*`). Both are excluded from every
spending total.

If a user or agent overrides the category, that classification is
discarded — an override always makes the row ordinary spending unless the
override's category string itself names an income category
(`"Payroll"`/`"Income"`). This is deliberate: an override exists precisely
to correct a wrong Plaid classification (e.g. Plaid tagged a real loan
payment as an internal transfer), and the corrected view must count as
real spending, not silently stay excluded because of the fact it just
overrode. See `test_override_reclassifies_a_transfer_as_real_spending` in
`tests/integration/test_analytics.py`.

## Refunds net against their category, not against "income"

A refund is a negative-amount transaction in the *same* category as the
original purchase. `get_spending_by_category` sums raw signed amounts per
category rather than filtering to positive amounts, so a refund
automatically nets against its purchase — a $42.10 restaurant charge and a
$15.00 refund in the same period net to $27.10 of restaurant spending,
with no special-case refund logic anywhere. A category that nets to
exactly zero within the period is omitted from the breakdown; a category
that nets *negative* (e.g. a refund with no matching purchase in the same
period) is not — that's a real, informative number, not noise.

## Pending transactions are included

A pending transaction is a real, bank-reported hold on the account.
Excluding it would understate current-period spending until the
transaction settles — sometimes by several days — so every total here
includes pending rows. `plaid.transactions.pending` remains available if
a future caller ever needs to distinguish settled from pending spend.

## Half-open date ranges, no timezone conversion

Every period is `[start, end)` (see `analytics/periods.py`) so a
transaction dated exactly on a month boundary is never double-counted or
dropped. `plaid.transactions.date` is a plain `Date` — Plaid reports the
transaction's local calendar date, not an instant — so analytics code
never converts a timezone; there isn't one to convert.

## Golden dataset

`tests/integration/test_analytics.py` seeds
`tests/plaid_fixtures/synthetic.golden_month` and asserts hand-computed
totals for January 2026:

| | |
|---|---|
| Rent | 1500.00 |
| Cash Advance | 100.00 |
| Groceries | 87.32 |
| Restaurants | 27.10 (42.10 purchase − 15.00 refund) |
| Subscription | 15.99 |
| Coffee Shop | 6.25 (pending) |
| **Total spending** | **1736.66** |
| Income (Payroll) | 3200.00 |
| Net cash flow | 1463.34 |
| Transfer (excluded from both) | 500.00 |

Any change to `analytics/*.py` that shifts one of these numbers is a
correctness regression until proven otherwise (handoff §29 Milestone 3
exit criteria: "golden synthetic dataset returns exact expected values").

## Recurring detection is a heuristic, not a fact

`analytics/recurring.py` flags a merchant as recurring when it billed a
similar amount (within 5% of the median) in at least two distinct
calendar months within the lookback window. This is presentation-layer
convenience, not a Plaid-verified subscription — callers must present it
as "looks recurring", never as certainty (handoff §8.2).

## Not yet built

- `finance spending`/`income`/`cashflow`/`budget` CLI commands
  (Milestone 4) — this package has no CLI surface yet, only the Python
  API the CLI and, eventually, the runtime agent's semantic tools will
  call.
- Agent-facing semantic tools (`get_spending_by_category`, etc. —
  Milestone 5) wrap these functions; they do not reimplement them.
