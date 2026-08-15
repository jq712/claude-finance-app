# ADR-008: Immutable container releases and rollback

**Status:** Accepted

## Context

Autonomous deployment is only safe if undoing a deployment is trivial and unambiguous. A mutable `latest` tag makes "what is actually running?" unanswerable and rollback a rebuild.

## Decision

Every release is a container image tagged by Git SHA. `latest` is never the sole production identifier. Both the current and previous known-good release are tracked so `finops rollback` is one step. Post-deploy health checks trigger automatic rollback on defined failure criteria.

## Consequences

- "What is running?" is always answerable, and maps to an exact revision.
- Rollback needs no rebuild and no network fetch beyond the registry.
- Migrations must be forward-compatible with the previous application version, or rollback is a lie — the database agent enforces this in review.

## Revisit when

Never.
