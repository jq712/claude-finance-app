# ADR-002: Modular monolith architecture

**Status:** Accepted

## Context

One user, one database, one VPS. The engineering agent is highly autonomous, which makes operational surface area expensive: every additional deployable is another thing that can be misconfigured without a human noticing.

## Decision

A single Python application, `src/finance_app/`, with enforced internal module boundaries (`plaid`, `db`, `analytics`, `agent`, `cli`, `ops`, `config`). One deployable artifact. No microservices, no service mesh, no Kubernetes.

## Consequences

- One image to build, deploy, roll back, and reason about.
- Module seams are maintained by review and import discipline rather than by network boundaries — the architecture agent owns this.
- Both the CLI and the conversational agent sit on the same `analytics/` layer, so there is exactly one implementation of every financial calculation.

## Revisit when

A component develops genuinely different scaling or availability requirements. Single-user workload makes this unlikely.
