# Architecture Decision Records

One decision per file, numbered and immutable. To change a decision, add a new ADR that supersedes the old one and mark the old one `Superseded by ADR-NNN` — do not rewrite history.

Format: **Context** (what forced the decision) · **Decision** (what we chose) · **Consequences** (what we accept, including the downsides) · **Revisit when** (the condition that would reopen it).

| ADR | Decision |
|---|---|
| [001](ADR-001-postgresql-primary-database.md) | PostgreSQL as the primary database |
| [002](ADR-002-modular-monolith.md) | Modular monolith architecture |
| [003](ADR-003-plaid-transactions-sync.md) | Plaid Transactions Sync as the ingestion mechanism |
| [004](ADR-004-raw-plaid-data-immutable.md) | Raw Plaid data is immutable to the LLM agent |
| [005](ADR-005-semantic-tools-no-agent-sql.md) | Semantic financial tools; no arbitrary agent SQL |
| [006](ADR-006-deterministic-financial-arithmetic.md) | Deterministic financial arithmetic |
| [007](ADR-007-git-cicd-only-production-path.md) | Git and CI/CD are the only normal path to production |
| [008](ADR-008-immutable-releases-rollback.md) | Immutable container releases and rollback |
| [009](ADR-009-isolated-dev-plaid-sandbox.md) | Isolated development environment with Plaid Sandbox |
| [010](ADR-010-production-secrets-excluded-from-engineering.md) | Production secrets excluded from the engineering session |
| [011](ADR-011-systemd-timers-scheduling.md) | systemd timers for scheduled single-host jobs |
| [012](ADR-012-daily-sync-reconciliation-fallback.md) | Daily sync remains the reconciliation fallback |
| [013](ADR-013-claude-code-engineering-openai-runtime.md) | Claude Code for engineering, OpenAI for the runtime agent (runtime-provider portion superseded by ADR-014) |
| [014](ADR-014-provider-interchangeable-runtime-agent.md) | Provider-interchangeable runtime financial agent (OpenAI or Claude API) |
| [015](ADR-015-backup-encryption-gpg-symmetric.md) | Backup encryption via GPG symmetric encryption, keyed by a single systemd credential |
| [016](ADR-016-production-deployment-control-plane.md) | Production deployment control plane: Compose interpolation, no long-running `app` until Milestone 8, and release-promotion concurrency |
| [017](ADR-017-autonomous-continuation-policy.md) | Autonomous continuation policy: Class A auto-merge, Class B/C hold for a human, merge is never deploy |
