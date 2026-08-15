# finance-app

A single-user, headless financial intelligence application. One Plaid Item → PostgreSQL → deterministic analytics → a CLI plus a conversational financial agent, running on one Linux VPS.

**Status:** Milestone 4 complete. The `finance` CLI (`status`, `sync`, `spending`, `income`, `cashflow`, `budget`, `transactions recent`/`search`) exposes the Milestone 3 analytics layer end to end and requires no LLM. See [`CLAUDE_FINANCE_APP_HANDOFF.md`](CLAUDE_FINANCE_APP_HANDOFF.md) §29 for the milestone plan, [`docs/database.md`](docs/database.md) for the schema, [`docs/plaid-sync.md`](docs/plaid-sync.md) for the sync design, [`docs/analytics.md`](docs/analytics.md) for the analytics design, and [`docs/cli.md`](docs/cli.md) for the CLI.

## The core idea

```
Plaid fact + user/agent interpretation = effective financial view
```

Raw Plaid records are immutable source-of-truth facts, written only by deterministic ingestion code. Categorizations, notes, tags, and budgets are interpretation, stored separately. Provenance is never destroyed, and the conversational agent can never overwrite a fact.

The second idea, equally load-bearing: **the model explains numbers, it never produces them.** Every figure the agent reports traces to a deterministic SQL or Python calculation. The LLM chooses which business question to ask; typed code answers it.

## Two agents, two providers

| | Engineering | Runtime |
|---|---|---|
| Who | Claude Code + specialist subagents | The `finance chat` financial agent |
| Provider | Anthropic | OpenAI |
| Sees | Sandbox credentials, synthetic data | Real financial data, semantic tools only |
| Never | Production Plaid secrets | Raw SQL, shell, Plaid credentials, `plaid.*` writes |

These are separate systems with separately compartmentalized authority. See [ADR-013](docs/adr/ADR-013-claude-code-engineering-openai-runtime.md).

## Documentation

| Document | What it covers |
|---|---|
| [`CLAUDE_FINANCE_APP_HANDOFF.md`](CLAUDE_FINANCE_APP_HANDOFF.md) | The authoritative brief — mission, invariants, milestones |
| [`CLAUDE.md`](CLAUDE.md) | Always-loaded working rules for Claude Code sessions |
| [`docs/architecture.md`](docs/architecture.md) | System shape, module boundaries, data flow |
| [`docs/security-model.md`](docs/security-model.md) | Trust boundaries, threat model, invariants |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

## Repository layout

```
CLAUDE.md                 always-loaded project rules
.claude/
  settings.json           permissions + hooks (committed, reviewable)
  agents/                 specialist subagents
  hooks/                  mechanical enforcement of the NEVER list
docs/
  architecture.md
  security-model.md
  adr/                    numbered decision records
src/finance_app/          (Milestone 0+) the application
migrations/               (Milestone 1) Alembic
tests/                    (Milestone 0+) unit, integration, security, evals
deploy/                   (Milestone 7) Compose, systemd, Caddy
```

## Getting started

```bash
uv sync
docker compose -f deploy/compose.dev.yaml up -d
uv run alembic upgrade head
uv run pytest
uv run ruff format . && uv run ruff check .
uv run pyright
```

The next task is Milestone 5: the runtime financial agent (OpenAI-backed) — semantic tools over `user.*`/`finance.*`/`agent.*`, read-only on `plaid.*`, no `run_sql`. Start a Claude Code session in this directory and:

```text
Continue executing CLAUDE_FINANCE_APP_HANDOFF.md from the first incomplete milestone.
```

## Guardrails you will hit

`.claude/settings.json` denies reads of credential paths and blocks `ssh`/`scp` — the engineering environment has no path to production. A `PreToolUse` hook refuses edits to committed Alembic migrations; corrections go forward as new revisions. These encode invariants from handoff §4. If one blocks you, the approach is wrong, not the guardrail.

## Security

Development uses Plaid Sandbox and synthetic fixtures exclusively. Production credentials exist only inside the production runtime boundary as systemd encrypted credentials — never in this repository, in CI, or in an agent session. Never commit a real credential or real financial data.
