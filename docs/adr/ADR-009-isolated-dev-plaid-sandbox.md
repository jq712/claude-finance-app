# ADR-009: Isolated development environment with Plaid Sandbox

**Status:** Accepted

## Context

Maximum engineering autonomy is desirable. Maximum autonomy against real financial data and live Plaid credentials is not.

## Decision

Development and CI use Plaid Sandbox credentials, synthetic fixtures, and disposable PostgreSQL containers exclusively. No real financial payload ever enters `tests/`, `evals/`, or any fixture. The development environment never mounts or decrypts production secrets.

## Consequences

- The agent can be given broad latitude in the dev environment because the blast radius is synthetic.
- Some production-only behaviors (real institution quirks, live re-auth flows) surface only in production and need runbooks rather than tests.
- Golden synthetic datasets must be rich enough to be a real test — refunds, transfers, pending replacements, removed records, ambiguous merchants.

## Revisit when

Never.
