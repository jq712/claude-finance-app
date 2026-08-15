# ADR-003: Plaid Transactions Sync as the ingestion mechanism

**Status:** Accepted

## Context

Plaid offers a cursor-based incremental endpoint (`/transactions/sync`) and older date-range endpoints. Transaction data is not append-only: records are modified, removed, and transition from pending to posted.

## Decision

Use `/transactions/sync` with a durable cursor, handling `added`, `modified`, and `removed` explicitly. A daily systemd timer performs at least one sync.

## Consequences

- Modifications and removals arrive as first-class events rather than being inferred by diffing full pulls.
- The cursor becomes the correctness hinge: it must advance only behind durably persisted writes (ADR-012 covers the reconciliation fallback).
- Every sync change requires tests for all three event types — this is in `CLAUDE.md` as a standing rule.
- Pending-to-posted transitions must be handled as replacement, not duplication.

## Revisit when

Plaid deprecates or materially changes the sync contract.
