# ADR-012: The daily sync remains the reconciliation fallback even with webhooks

**Status:** Accepted

## Context

Plaid's `SYNC_UPDATES_AVAILABLE` webhook gives fresher data than polling. But webhook delivery is best-effort: it can be dropped, delayed, or missed while the endpoint is down or the certificate has expired. A webhook-only design fails silently, which is the worst failure mode for financial data.

## Decision

The daily systemd timer is mandatory and permanent. Webhooks are an enhancement that triggers an earlier sync; they never replace the scheduled reconciliation.

## Consequences

- Maximum silent staleness is bounded at roughly 24 hours regardless of webhook health.
- The webhook path stays trivial — validate, then request a sync — because it is not load-bearing for correctness.
- The sync operation must be safely re-entrant, since the timer and a webhook can fire close together. An advisory lock plus idempotent upserts handle this.
- v1 ships with the daily sync alone; webhooks are Milestone 8 and must not delay it.

## Revisit when

Never.
