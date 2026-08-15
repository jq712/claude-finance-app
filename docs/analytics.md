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
  override's category string wins outright, and transfer/income
  classification is re-derived from the *override string itself* — see
  below.

## Transfers and income are excluded from spending, and an override wins in either direction

A transaction is a transfer or income based on Plaid's raw classification
(`personal_finance_category.primary` in `{TRANSFER_IN, TRANSFER_OUT}`, or
the legacy top-level `plaid_category[0]` being `"Transfer"`; income is the
equivalent for `Payroll`/`INCOME_*`). Both are excluded from every
spending total.

If a user or agent overrides the category, that raw classification is
discarded — but the override string is checked case-insensitively against
the same transfer/income sentinel names (`"Transfer"`, `"Payroll"`,
`"Income"`). This has to work in both directions:

- An override *away* from `"Transfer"` to a real category (e.g. Plaid
  tagged a loan payment as an internal transfer) must count as real
  spending — the correction is the whole point of overriding.
- An override *to* `"Transfer"` or `"Income"` (a user correcting the
  opposite mistake — Plaid missed that a transaction was an internal
  transfer or a paycheck) must exclude it from spending exactly as a raw
  Plaid classification would. Case-insensitively, because an override
  string can come from free text, a CLI flag, or an agent tool with no
  enforced casing.

See `test_override_reclassifies_a_transfer_as_real_spending` and the
`test_override_named_transfer_*`/`test_override_income_*` tests in
`tests/integration/test_analytics.py` and
`test_analytics_adversarial.py`.

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

Grouping is by `merchant_name`, falling back to the raw `name` when
Plaid's merchant enrichment didn't populate it for that occurrence —
enrichment is probabilistic and can be present one month and absent the
next for the same real merchant. To avoid splitting one recurring series
into two under-threshold fragments, every occurrence sharing a raw `name`
within the lookback window is resolved to the same grouping key by
backfilling any `merchant_name` seen for that `name`.

## Known limitations

**Category-label drift between periods.** `compare_periods` and
`get_budget_status` match category labels case-insensitively, but not
across genuinely different label strings for the same real-world
category. If Plaid reports a merchant via the legacy `plaid_category`
list in one period and switches to `personal_finance_category` in
another (a realistic mid-history taxonomy migration), `_raw_label`
derives two different strings (`"Restaurants"` vs. `"Food And Drink
Restaurants"`) and a category-scoped comparison silently reports the
later period as zero spend in the earlier label, rather than reconciling
the two or erroring. Fixing this needs a canonical category-taxonomy
mapping, which is out of scope for Milestone 3. Pinned by
`test_compare_periods_accepted_limitation_label_source_drift_between_periods`
in `tests/integration/test_analytics_adversarial.py`.

**A stale override silently re-applies to a resurrected transaction.** A
category override survives `mark_removed`'s tombstone (it isn't cleared
when the transaction it points to is removed), and Milestone 1/2's
`upsert` unconditionally clears `removed_at` on any later `added`/
`modified` replay for the same `plaid_transaction_id`. If Plaid removes a
transaction and later re-adds an id that was previously overridden, the
old override reattaches with no audit trail that this happened. Whether
this is correct (same real transaction, override still valid) or wrong
(the resurrected row may not be the same real-world event) is a product
decision, not an obvious bug — see the equivalent Milestone 1/2 gap
documented in `docs/plaid-sync.md`'s Known limitations. Pinned by
`test_stale_override_silently_reapplies_when_a_removed_transaction_is_resurrected`.

## Not yet built

- `finance spending`/`income`/`cashflow`/`budget` CLI commands
  (Milestone 4) — this package has no CLI surface yet, only the Python
  API the CLI and, eventually, the runtime agent's semantic tools will
  call.
- Agent-facing semantic tools (`get_spending_by_category`, etc. —
  Milestone 5) wrap these functions; they do not reimplement them.
