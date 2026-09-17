# AGENTS.md — Project Rules

Authoritative brief: `CLAUDE_FINANCE_APP_HANDOFF.md`. Read it before any architectural work.
This file is the short, always-loaded version. Where the two disagree, the handoff wins and this file is wrong and must be fixed.
This file is the OpenCode port of `CLAUDE.md` (which remains as a record and for Claude Code compatibility); where AGENTS.md and CLAUDE.md disagree, the handoff wins and AGENTS.md is wrong and must be fixed.

## Mission

A secure, single-user, headless financial intelligence application. One Plaid Item → PostgreSQL → deterministic analytics → CLI plus a conversational agent. Production-grade, handling real personal financial data.

## Architecture

- Python, modular monolith, `src/finance_app/`
- PostgreSQL (schemas: `plaid`, `user`, `finance`, `agent`, `ops`)
- SQLAlchemy 2.x + psycopg, Alembic migrations
- Typer + Rich CLI; `finance` (user) and `finops` (operations)
- The **runtime** financial agent is provider-interchangeable (OpenAI or the Claude API, via `AGENT_PROVIDER`) — see ADR-014 and handoff §8.4
- OpenCode is the **engineering** harness — always a different system from the runtime agent, even when the runtime agent is configured to use a Claude-family model. Never conflate them.
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

**The merge itself is mechanical, not just a judgment call**: `gh pr merge` sits in `opencode.json`'s `permission.bash` ask list — the sanctioned path from an autonomous session to an actual Class A merge is `.opencode/scripts/merge-class-a.sh <PR>`, which independently re-verifies mergeable state, that every required check is actually green, and that the diff doesn't touch a path this repo treats as inherently non-Class-A (migrations, `deploy/`, `.claude/`, ADRs, the Plaid/agent boundaries, or the rule-defining docs themselves — `CLAUDE.md`, `AGENTS.md`, `CLAUDE_FINANCE_APP_HANDOFF.md`, `docs/security-model.md`) before it merges anything.

**When the script refuses a PR, that is the mechanism working, not a bug to route around.** A Class B PR — or any PR the script refuses on a reserved path — is merged by the **owner** with `gh pr merge` after every required check is green. The agent's job ends at the open PR: it does not merge, does not ask a parent model to merge, and does not edit the script's reserved-path list to make its own change mergeable.

## Delegation

Use the subagents in `.opencode/agents/` for their specialties: `architecture`, `database`,
`implementation`, `qa-adversarial`, `security-reviewer`, `sre-release` — plus `code-reviewer`,
the Kimi-backed substitute for Claude Code's `/code-review` that the `pre-merge-review` Skill
dispatches. Each agent's own `.opencode/agents/*.md` file documents its specialty and tool grants.
Never let the subagent that wrote a Class B change be the one that approves it — spawn
`security-reviewer` and `qa-adversarial` as **parallel** `task` calls (one message, both calls) so
independence doesn't cost wall-clock time. Review subagents have edit/write denied by design.

Use the Skills in `.opencode/skills/` for recurring procedures instead of re-deriving them from
memory each time: `autonomous-continuation` (backlog pickup, classification, session lifecycle),
`safe-migration`, `plaid-sync-review`, `release-readiness`, and `pre-merge-review` (mandatory
fresh-context correctness pass before any unit of work — Class A included — is called done).

## Session lifecycle (unattended-safe)

These hold when nobody is watching a tool prompt. Each exists because the failure actually happened here.

- **Boot from git, never from memory.** At session start run `git fetch origin`, `git status`, `git branch --show-current`, `gh pr list --state open`. Never start work on a branch behind `origin/main`; branch fresh off latest `origin/main` instead. (A checkout once ran 28 commits behind against a mismatched database schema.)
- **Recover worktree work via git, never by copying files.** If work exists only in a dirty/locked worktree, `git show` the commit or `git cherry-pick` it onto a fresh branch. Never copy files out of a locked worktree — it can carry uncommitted, half-applied state that silently reverts already-merged fixes.
- **Harness config is cached at OpenCode start.** Plugins (`guards.js`) and subagent `model:` pins load once, so restart OpenCode after editing either and do not trust a mid-session edit. Guard changes must fail closed — never change a guard to skip or fail open — and never leave a temporary hook stub on disk or commit one.
- **Only a subagent's actual output is a review.** The parent session must never impersonate `code-reviewer`, `security-reviewer`, or `qa-adversarial`, or report a review that did not run. If a mandated reviewer fails to dispatch (e.g. a provider auth error), the review did not happen: record the blocker in `STATUS.md` (or `OVERNIGHT.md` for an overnight run) and stop — never self-review in the authoring context and proceed as if the gate passed.
- **The Kimi specialist gate is Class B only.** `security-reviewer` + `qa-adversarial` (both `moonshotai/kimi-k3`) are required for every Class B PR and are not widened to Class A; `pre-merge-review`'s `code-reviewer` is the every-class gate. Verify the model pin matches the provider actually available before calling a review complete.
- **When blocked with nobody present, stop cleanly.** No idle waits, no polling, no loop-retries, no invented or self-approved gates. Record the blocker and the recommended default (PR description, commit message, or `gh issue create`), finish any other unblocked work, and end the session as exactly one of: a merged Class A PR, an open PR, or a durable note (ADR-017, handoff §32).

## Context discipline

- **What must survive compaction**: current branch, modified files, open PR links, this unit of
  work's risk class, and the exact test commands already run. Re-query these from `git`/`gh`
  (`git status`, `git diff`, `gh pr list`, `gh pr view`) rather than trusting recollection —
  they're the actual source of truth, before or after compaction.
- **Delegate research, don't inline it.** Understanding an unfamiliar module, verifying an
  OpenCode/Plaid/GitHub Actions API detail (handoff §32), or any multi-file investigation
  belongs in a subagent or fork, not read wholesale into the lead session's own context.
- **The `plan` agent** for multi-file or architecturally uncertain work; **direct execution** for
  a single-file, well-scoped, low-risk change. Don't switch to the `plan` agent for a one-line
  fix; don't skip planning a multi-module change to move faster.
- **Fresh-context correctness review is mandatory before "done"** — the `pre-merge-review` Skill,
  scoped to correctness findings only. Every other finding class (style, simplification, reuse,
  efficiency) is explicitly optional and non-blocking.

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
- Unattended commits go **only** through `.opencode/scripts/commit.sh "msg"` — it refuses `main`, detached HEAD, empty messages, and messages beginning with `-`, and runs exactly `git commit -m "$1"`. Never `git commit --amend` and never `git commit -n`/`--no-verify`; raw `git commit` stays `ask` in `opencode.json`.
- New work on a branch off latest `origin/main`.
- `git fetch origin` before branch or cherry-pick.
- Open PRs with `gh pr create --base main`.
- Merge by class: **Class A** only via `.opencode/scripts/merge-class-a.sh` — never bare `gh pr merge` from an agent session (it sits in the ask list and stalls unattended). **Class B, and any PR the script refuses on a reserved path, is merged by the owner with `gh pr merge` after green CI**; the agent stops at the open PR and never merges it. The script's refusal is the mechanism, not an obstacle.
- Do not work in or copy from dirty/locked worktrees; recover their work via `git show`/`git cherry-pick` (see Session lifecycle).
- Read STATUS.md at session start. If it disagrees with git, trust git and fix STATUS.md.
- After each task, update STATUS.md "Last session".

## Definition of done

Implementation · types/lint clean · unit tests · integration tests · migration if schema changed · agent eval if prompts or tools changed · docs updated if behavior changed · runbook updated if operations changed · **`README.md`'s Status line updated if this PR completes or begins a milestone** (CI's `docs-freshness` job checks this mechanically for milestone-titled/milestone-branched PRs — it has gone stale before, don't rely on memory) · fresh-context correctness review via `.opencode/skills/pre-merge-review` (every class, not just Class B) · CI green · no new secrets · clear rollback path.

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
