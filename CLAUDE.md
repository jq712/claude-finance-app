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
- The **runtime** financial agent is provider-interchangeable (OpenAI or the Claude API, via `AGENT_PROVIDER`) — see ADR-014 and handoff §8.4
- Claude Code is the **engineering** agent — always a different system from the runtime agent, even when the runtime agent is configured to use a Claude-family model. Never conflate them.
- Plaid Transactions Sync (incremental, cursor-based), daily systemd timer
- No Docker (ADR-019, 2026-09-13 — supersedes the earlier Compose-based design in ADR-016). Bare-metal on a single Linux VPS, no Kubernetes: production is a release directory under `/opt/finance` with a `current` symlink, run by systemd units invoking a virtualenv directly. One host PostgreSQL instance, two databases (`finance_dev`, `finance_prod`)

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
- run arbitrary PR code against `/opt/finance` or its credentials — engineering and production share a VPS (ADR-007/ADR-010/ADR-019, revised 2026-09-13); the boundary is the `/opt/finance`/Unix-user separation, not the host
- edit code directly in `/opt/finance`, or read/decrypt production credentials from an engineering session — same reason
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

The runtime financial agent gets **semantic, parameterized tools** only — `get_spending_by_category`, `create_budget`, etc. There is no `run_sql(query: str)` and there never will be. Write tools validate inputs, are audited to `agent.tool_calls`, and touch only `user.*`, `finance.*`, and `agent.*`. Tool definitions are provider-neutral — defined once, translated to each provider's wire format by its adapter (ADR-014) — never hand-duplicated per provider.

## Risk classes (see handoff §25)

- **Class A** — lint, formatting, clear test regressions, logging, patch deps. Fix and merge through normal gates (CI required-green — never bypassed). Merge is not deploy: production deploy is always an owner-performed `finops deploy`, for every class, with no exception for Class A.
- **Class B** — migrations, Plaid sync semantics, financial math, credentials, webhook security, agent permissions. Requires implementation + specialist review + adversarial tests + security review + staging before production. Autonomous continuation implements and opens the PR, then stops and waits for the user to merge — never auto-merged.
- **Class C** — suspected credential compromise, data corruption, lost source-of-truth records, failed restores. **Stop destructive automation. Preserve evidence. Write an incident report. Escalate to the owner.** Refusing an unsafe mutation is the correct autonomous action.

Class A auto-merge is the standing default whenever a session is asked to continue the milestone backlog autonomously (handoff §34) — interactive, resumed, or scheduled makes no difference. Full contract, including what to do when nobody is present to answer a blocker: ADR-017 and handoff §32.

## Delegation

Use the subagents in `.claude/agents/` for their specialties. Never let the subagent that wrote a Class B change be the one that approves it — spawn `security-reviewer` and `qa-adversarial` as separate invocations. Review subagents have no write tools by design.

## Working agreements

- PR #15 (`rebuild-autonomous-engineering-workflow`) is Jordan working solo — do not pick it up, review it, or merge it unless explicitly asked. Remove this note once that PR is closed.
- Branch per unit of work; PR into `main`. No direct commits to `main`.
- `uv` for dependency and environment management.
- `ruff format` + `ruff check` + `pyright` must be clean before a change is done.
- Tests run against a real PostgreSQL instance (`finance_dev`, or CI's own Postgres service container), not SQLite.
- Plaid Sandbox credentials only. If a task appears to need production Plaid credentials, stop and escalate — that is a design error, not a credential problem.
- Do not add a dependency, service, or framework without answering: what concrete problem does it solve *now*, can Postgres/systemd/plain Python do it more simply, and what new failure mode does it introduce? (No Docker — ADR-019.)

## Git

- Use `gh` for PRs, checks, and branch delete.
- Never commit or push `main`. Never force-push.
- New work on a branch off latest `origin/main`.
- `git fetch origin` before branch or cherry-pick.
- Open PRs with `gh pr create --base main`.
- Merge by class: **Class A** only via `.opencode/scripts/merge-class-a.sh` — never bare `gh pr merge` from an agent session (it sits in the ask list and stalls unattended). **Class B, and any PR the script refuses on a reserved path, is merged by the owner with `gh pr merge` after green CI**; the agent stops at the open PR and never merges it. The script's refusal is the mechanism, not an obstacle.
- Do not work in or copy from dirty/locked worktrees; recover their work via `git show`/`git cherry-pick`.
- Read STATUS.md at session start. If it disagrees with git, trust git and fix STATUS.md.
- After each task, update STATUS.md "Last session".

## Definition of done

Implementation · types/lint clean · unit tests · integration tests · migration if schema changed · agent eval if prompts or tools changed · docs updated if behavior changed · runbook updated if operations changed · **`README.md`'s Status line updated if this PR completes or begins a milestone** (CI's `docs-freshness` job checks this mechanically for milestone-titled/milestone-branched PRs — it has gone stale before, don't rely on memory) · CI green · no new secrets · clear rollback path.

## Commands

```bash
uv sync                      # install
uv run pytest                # tests
uv run ruff format . && uv run ruff check --fix .
uv run pyright
# dev Postgres: a host-installed PostgreSQL instance, `finance_dev` database
# (ADR-019 — no Docker; no `docker compose -f deploy/compose.dev.yaml` anymore)
uv run alembic upgrade head
```

(These become real during Milestone 0/1 — see handoff §29. If a command above does not exist yet, that is the next thing to build, not a reason to improvise around it.)
