# finance-app

A single-user, headless financial intelligence application. One Plaid Item → PostgreSQL → deterministic analytics → a CLI plus a conversational financial agent, running on one Linux VPS.

**Status:** Milestones 0–6 complete (database foundation, Plaid sync, deterministic analytics, CLI, provider-interchangeable conversational agent, agent eval framework). Milestone 7 (production deployment) is in progress, on `main` directly (no dedicated milestone branch — see ADR-019's implementation-status table for the authoritative per-piece breakdown). Shipped: the bare-metal `finops deploy`/`rollback`/`restart` rewrite (`src/finance_app/ops/host.py`), the non-forgeable release-identity check (`src/finance_app/ops/identity.py`), the explicit-prod-opt-in guard, `deploy/scripts/release.sh` (the release-copy step that puts a real release directory in place before `finops deploy` will touch it), and CI's Docker-shaped jobs redesigned — `Dockerfile`/`deploy/compose*.yaml` are removed, and a bare-metal `release-preflight` CI job (git-archive, build, migrate, `finance selfcheck`) replaces the old image build/publish/migration-preflight/staging-smoke jobs. Not yet shipped: the systemd unit files and real production provisioning on the VPS — `/opt/finance` doesn't exist yet, so no production deployment is running. **No Docker** (ADR-019, revised 2026-09-13, supersedes ADR-016's Compose-based design): the engineering workspace and production share one VPS, but production is a bare-metal `/opt/finance` release directory, never this repository, a second Claude Code worktree, or a container — see ADR-019 and `docs/security-model.md`. `finance chat` is a conversational interface to the analytics layer, running on a provider-interchangeable runtime agent (OpenAI or the Claude API, via `AGENT_PROVIDER` — [ADR-014](docs/adr/ADR-014-provider-interchangeable-runtime-agent.md)) with a fixed semantic tool set, read-only on `plaid.*`. The deterministic `finance` CLI (`status`, `sync`, `spending`, `income`, `cashflow`, `budget`, `transactions recent`/`search`) still requires no LLM. See [`CLAUDE_FINANCE_APP_HANDOFF.md`](CLAUDE_FINANCE_APP_HANDOFF.md) §29 for the milestone plan, [`docs/database.md`](docs/database.md) for the schema, [`docs/plaid-sync.md`](docs/plaid-sync.md) for the sync design, [`docs/analytics.md`](docs/analytics.md) for the analytics design, [`docs/cli.md`](docs/cli.md) for the CLI, [`docs/financial-agent.md`](docs/financial-agent.md) for the runtime agent, and [`docs/deployment.md`](docs/deployment.md) for the production topology.

## The core idea

```
Plaid fact + user/agent interpretation = effective financial view
```

Raw Plaid records are immutable source-of-truth facts, written only by deterministic ingestion code. Categorizations, notes, tags, and budgets are interpretation, stored separately. Provenance is never destroyed, and the conversational agent can never overwrite a fact.

The second idea, equally load-bearing: **the model explains numbers, it never produces them.** Every figure the agent reports traces to a deterministic SQL or Python calculation. The LLM chooses which business question to ask; typed code answers it.

## Two agents, always separate

| | Engineering | Runtime |
|---|---|---|
| Who | Claude Code + specialist subagents | The `finance chat` financial agent |
| Provider | Anthropic (fixed) | OpenAI or Anthropic, by `AGENT_PROVIDER` config |
| Sees | Sandbox credentials, synthetic data | Real financial data, semantic tools only |
| Never | Production Plaid secrets | Raw SQL, shell, Plaid credentials, `plaid.*` writes |

These are separate systems with separately compartmentalized authority, even when both happen to
run on a Claude-family model. See [ADR-013](docs/adr/ADR-013-claude-code-engineering-openai-runtime.md)
and [ADR-014](docs/adr/ADR-014-provider-interchangeable-runtime-agent.md).

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
deploy/                   (Milestone 7) systemd, Caddy — no Docker (ADR-019)
```

## Getting started

```bash
uv sync
# dev Postgres: a host-installed PostgreSQL instance, `finance_dev` database
# (ADR-019 — no Docker; no `docker compose -f deploy/compose.dev.yaml` anymore)
uv run alembic upgrade head
uv run pytest
uv run ruff format . && uv run ruff check .
uv run pyright
```

The next task is finishing Milestone 7 (production deployment — systemd units, then real provisioning on the VPS per `docs/runbooks/deploy.md`), then Milestone 8 (Plaid webhook). Start a Claude Code session in this directory and:

```text
Continue executing CLAUDE_FINANCE_APP_HANDOFF.md from the first incomplete milestone.
```

This runs under the standing autonomy contract in [ADR-017](docs/adr/ADR-017-autonomous-continuation-policy.md): Class A changes get implemented and merged autonomously once CI is green; Class B changes get implemented and reviewed but wait as an open PR for you to merge; Class C stops and leaves a note. Applies the same way whether the session is interactive or a scheduled routine — see [`docs/runbooks/autonomous-continuation.md`](docs/runbooks/autonomous-continuation.md) for what to check after a run.

## Guardrails you will hit

`.claude/settings.json` denies reads of credential paths and blocks `ssh`/`scp`/`systemd-creds` — the engineering session cannot read `/opt/finance` or its credentials, even though it shares a VPS with production (ADR-007/ADR-010/ADR-019, revised 2026-09-13 — see `docs/security-model.md`). A `PreToolUse` hook refuses edits to committed Alembic migrations; corrections go forward as new revisions. These encode invariants from handoff §4. If one blocks you, the approach is wrong, not the guardrail.

## Security

Development uses Plaid Sandbox and synthetic fixtures exclusively. Production credentials exist only inside `/opt/finance/.env` (mode 600) and systemd encrypted credentials — never in this repository, in CI, or in an agent session, even though the engineering workspace and production now share a VPS (`docs/security-model.md`). Never commit a real credential or real financial data.
