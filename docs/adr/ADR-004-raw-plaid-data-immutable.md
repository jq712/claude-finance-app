# ADR-004: Raw Plaid data is immutable to the LLM agent

**Status:** Accepted

## Context

The user wants to recategorize transactions, annotate them, and correct merchant names. The naive implementation mutates the transaction row. That destroys the distinction between what the bank reported and what the user believes, and makes reconciliation against Plaid impossible.

## Decision

```
Plaid fact + user/agent interpretation = effective financial view
```

`plaid.*` is written only by deterministic ingestion and reconciliation code. Interpretation lives in `user.transaction_category_overrides`, `user.transaction_tags`, `user.transaction_notes`. Analytics reads a view composing the two. Enforced by the `finance_agent` role having no write grant on the `plaid` schema.

## Consequences

- Removing an override restores the Plaid fact exactly, because it was never altered.
- Reconciliation against Plaid stays meaningful indefinitely.
- Every read path costs a join. Accepted.
- A test asserting the missing grant must exist and must fail loudly if it drifts.

## Revisit when

Never. This is the foundational invariant of the system.
