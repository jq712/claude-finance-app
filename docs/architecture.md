# Architecture

Derived from `CLAUDE_FINANCE_APP_HANDOFF.md`. When implementation diverges from this document, update this document in the same change.

## Shape

A modular monolith. One Python application, one PostgreSQL instance (`finance_dev`/`finance_prod` as separate logical databases on it — ADR-019), one Linux VPS shared by the engineering workspace and production (ADR-007/ADR-010/ADR-019, revised 2026-09-13). Scheduling lives in systemd timers rather than inside a long-running application scheduler, so a crashed process cannot silently stop the daily sync.

```
                     Private Git Repository
                              |
                        GitHub Actions
                              |
        known git ref, copied to /opt/finance/releases/<sha>
                  (ADR-019, revised 2026-09-13 — no Docker)
                              |
                              v
+----------------------------------------------------------------+
|                          Linux VPS                             |
|                                                                |
|  +--------------------+        +----------------------------+  |
|  | Python application |------->| PostgreSQL                 |  |
|  |                    |        |                            |  |
|  | finance CLI        |        | plaid.*   source of truth  |  |
|  | chat agent         |        | user.*    overrides        |  |
|  | Plaid sync         |        | finance.* budgets          |  |
|  | analytics          |        | agent.*   audit            |  |
|  | finops CLI         |        | ops.*     operational      |  |
|  +---------+----------+        +----------------------------+  |
|            |                                                   |
|            +---------------------> OpenAI API or Claude API    |
|            |                       (AGENT_PROVIDER, ADR-014)   |
|            +---------------------> Plaid API                   |
|                                                                |
|  systemd timers: daily sync, backup, health/reconciliation     |
|  Caddy: only if the public webhook endpoint is enabled         |
|  encrypted machine credentials                                 |
+----------------------------------------------------------------+
```

The user reaches the CLI over SSH. Nothing else is publicly reachable except, optionally, the narrow Plaid webhook endpoint.

## Module boundaries

| Module | Owns | Must not |
|---|---|---|
| `plaid/` | Plaid client, `/transactions/sync` cursor loop, webhook handling, reconciliation | Contain analytics or presentation logic |
| `db/` | Models, repositories, queries, views, session management | Be called directly by the agent's tool layer |
| `analytics/` | Spending, income, cashflow, recurring detection, budgeting — all deterministic | Call an LLM, ever |
| `agent/` | Tool definitions, instructions, guardrails, provider-interchangeable `AgentProvider` adapters (ADR-014) | Compute financial figures itself, or reach `db/` outside a semantic tool |
| `cli/` | `finance` commands and the chat loop | Contain business logic that the agent also needs — that belongs in `analytics/` |
| `ops/` | Health, status, structured logging, `finops` | Expose financial payloads |
| `config/` | Settings, credential loading, environment validation | Read production secrets in a non-production environment |

The seam that matters most: **`analytics/` is the only place financial figures are computed, and `agent/` reaches data only through the semantic tools.** Both the CLI and the agent sit on top of the same deterministic layer, which is why the application stays useful when the runtime LLM (whichever provider is configured) is unavailable.

## Data flow: ingestion

```
systemd timer (daily)  ──┐
                         ├──> sync run (run_id, advisory lock)
Plaid SYNC_UPDATES_AVAIL ┘         |
                                   v
                   /transactions/sync (cursor)
                                   |
                    added / modified / removed
                                   |
                   deterministic upsert into plaid.*
                                   |
                   advance cursor only after the page's
                   writes are durably persisted
                                   |
                        record in ops.sync_runs
```

The cursor is the correctness hinge. It advances only behind persisted writes, so an interrupted run replays rather than skips. Plaid's natural keys are unique-constrained, so a replay upserts instead of duplicating. The daily timer remains the reconciliation safety net even after webhooks exist.

## Data flow: a question

```
"How much did I spend eating out over the last six months?"
        |
        v
  agent selects a semantic tool
        |
        v
  get_spending_by_category(category="restaurants", interval="month", ...)
        |
        v
  analytics/ + db/  ->  deterministic monthly figures
        |
        v
  compact structured result returned to the model
        |
        v
  model explains the trend; numbers are quoted, not computed
```

## Effective financial view

```
Plaid fact + user/agent interpretation = effective financial view
```

`plaid.transactions` holds what the institution reported. `user.transaction_category_overrides`, `user.transaction_tags`, and `user.transaction_notes` hold interpretation. Analytics reads a view that composes the two. Removing an override restores the Plaid fact exactly, because the fact was never modified.

This is why there is no in-place category column on the raw table, and why the `finance_agent` database role has no write grant on the `plaid` schema.

## Deployment

Git is the source of truth. Code reaches production only as a known, CI-green git ref copied into `/opt/finance/releases/<sha>/` (ADR-019, revised 2026-09-13 — no Docker, no image, supersedes ADR-016's image-based design), via CI → staging → smoke tests → critical evals → production → post-deploy health checks → automatic rollback on defined failure. Current and previous known-good releases are always tracked, via a `current` symlink rather than an image tag. There is no editing `/opt/finance` directly, and no self-hosted CI runner on the VPS. The engineering workspace and the production deployment share that VPS (ADR-007/ADR-010, revised 2026-09-13) — see `docs/security-model.md`'s "Trust boundaries" for what separates them now that it isn't a separate host.

## Deliberate omissions

No Kubernetes, service mesh, Redis, Celery, Kafka, or message queue. No microservices. No second orchestration framework. Single-user scale does not justify their failure modes, and each one widens the attack surface around real financial data. Concurrency control, where needed, is a PostgreSQL advisory lock. Revisit only against a concrete, observed requirement — and record it as an ADR.
