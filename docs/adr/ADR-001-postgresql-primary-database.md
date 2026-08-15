# ADR-001: PostgreSQL as the primary database

**Status:** Accepted

## Context

The system stores one person's transaction history plus derived budgets, overrides, and operational state. It needs exact decimal arithmetic, real constraints, transactional ingestion, role-based access control fine enough to forbid the runtime agent from writing source-of-truth rows, and durable single-host operation.

## Decision

PostgreSQL is the only datastore. Logical separation by schema: `plaid`, `user`, `finance`, `agent`, `ops`.

## Consequences

- `NUMERIC` gives exact money arithmetic without float error.
- Per-schema `GRANT`s enforce the agent's write boundary in the database rather than in application code, which is the difference between a rule and an invariant (ADR-004).
- Advisory locks handle sync concurrency, removing the need for a queue.
- SQLite is unusable even for tests — it lacks the roles and schema semantics the security model depends on.

## Revisit when

Never, realistically. A second datastore would need to justify splitting the transactional boundary.
