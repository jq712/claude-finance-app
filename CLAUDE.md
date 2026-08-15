# CLAUDE.md — Project Rules

Authoritative brief: `CLAUDE_FINANCE_APP_HANDOFF.md`. Read it before any architectural work.
This file is the short, always-loaded version. Where the two disagree, the handoff wins and this file is wrong and must be fixed.

## Mission

A secure, single-user, headless financial intelligence application. One Plaid Item → PostgreSQL → deterministic analytics → CLI plus a conversational agent. Production-grade, handling real personal financial data.

## Architecture

- Python, modular monolith, `src/finance_app/`
- PostgreSQL (schemas: `plaid`, `user`, `finance`, `agent`, `ops`)
- SQLAlchemy 2.x + psycopg, Alembic migrations
- Typer + Rich CLI; `finance` (user) and `finops` (operations)
- OpenAI models power the **runtime** financial agent
- Claude Code is the **engineering** agent — these are different systems, do not conflate them
- Plaid Transactions Sync (incremental, cursor-based), daily systemd timer
- Docker + Compose, single Linux VPS, no Kubernetes

## The one idea that matters

```
Plaid fact + user/agent interpretation = effective financial view
```

Raw Plaid rows are immutable source-of-truth facts. Interpretation lives in separate override/annotation tables. Provenance is never destroyed.

## NEVER

- access or request production Plaid secrets during development
- commit credentials, tokens, or real `.env` files
- put real financial payloads in tests or fixtures — synthetic only
- mutate raw Plaid rows (`plaid.*`) from the LLM agent or its tools
- expose arbitrary SQL, shell, or filesystem access to the LLM agent
- rewrite an already-applied migration
- bypass CI to merge or deploy
- disable, skip, or weaken a test merely to make a change pass
- force-push `main`
- run arbitrary PR code on the production VPS
- edit code directly on the production VPS
- let a model be the sole reviewer of its own consequential change

## ALWAYS

- use migrations for schema changes
- preserve raw Plaid provenance
- test `added` / `modified` / `removed` sync behavior for any sync change
- use parameterized queries
- compute money in SQL/Python, never in the model — `NUMERIC`/`DECIMAL` or integer minor units, never binary floats
- add a regression test for every bug fixed
- update docs / ADRs / runbooks when architectural behavior changes
- keep production deployment reproducible and rollbackable

## Financial correctness

The model explains numbers; it never produces them. Every numeric claim the agent makes must trace to a deterministic query or analytics function. If you find yourself asking the LLM to add, average, or compare amounts, you have made a mistake.

## Agent tool boundary

The runtime financial agent gets **semantic, parameterized tools** only — `get_spending_by_category`, `create_budget`, etc. There is no `run_sql(query: str)` and there never will be. Write tools validate inputs, are audited to `agent.tool_calls`, and touch only `user.*`, `finance.*`, and `agent.*`.

## Risk classes (see handoff §25)

- **Class A** — lint, formatting, clear test regressions, logging, patch deps. Fix and ship through normal gates.
- **Class B** — migrations, Plaid sync semantics, financial math, credentials, webhook security, agent permissions. Requires implementation + specialist review + adversarial tests + security review + staging before production.
- **Class C** — suspected credential compromise, data corruption, lost source-of-truth records, failed restores. **Stop destructive automation. Preserve evidence. Write an incident report. Escalate to the owner.** Refusing an unsafe mutation is the correct autonomous action.

## Delegation

Use the subagents in `.claude/agents/` for their specialties. Never let the subagent that wrote a Class B change be the one that approves it — spawn `security-reviewer` and `qa-adversarial` as separate invocations. Review subagents have no write tools by design.

## Working agreements

- Branch per unit of work; PR into `main`. No direct commits to `main`.
- `uv` for dependency and environment management.
- `ruff format` + `ruff check` + `pyright` must be clean before a change is done.
- Tests run against a real PostgreSQL container, not SQLite.
- Plaid Sandbox credentials only. If a task appears to need production Plaid credentials, stop and escalate — that is a design error, not a credential problem.
- Do not add a dependency, service, or framework without answering: what concrete problem does it solve *now*, can Postgres/systemd/Docker/plain Python do it more simply, and what new failure mode does it introduce?

## Definition of done

Implementation · types/lint clean · unit tests · integration tests · migration if schema changed · agent eval if prompts or tools changed · docs updated if behavior changed · runbook updated if operations changed · CI green · no new secrets · clear rollback path.

## Commands

```bash
uv sync                      # install
uv run pytest                # tests
uv run ruff format . && uv run ruff check --fix .
uv run pyright
docker compose -f deploy/compose.dev.yaml up -d    # dev Postgres
uv run alembic upgrade head
```

(These become real during Milestone 0/1 — see handoff §29. If a command above does not exist yet, that is the next thing to build, not a reason to improvise around it.)
