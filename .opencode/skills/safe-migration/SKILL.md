---
name: safe-migration
description: Checklist for authoring or reviewing an Alembic migration in this repo (money types, additive-first sequencing, Plaid natural-key uniqueness, tested downgrade path, the applied-migration edit guard). Use whenever creating a new migration under migrations/versions/, or reviewing one before it merges.
---

# Safe migration authoring

Consolidates the `database` agent's review checklist and handoff §4.9/§7. Every consequential
schema change is Class B (AGENTS.md's Risk classes) — implementation, then independent
`database`-agent review, then the full ADR-017 gate before it ships. This Skill is what both the
author and the reviewer check against; do not re-derive it from memory.

## Before writing the migration

- **Never edit a committed migration file.** The guards plugin (`.opencode/plugins/guards.js`,
  running the same guard script Claude Code's `PreToolUse` hook used) blocks this mechanically —
  if you hit it, the fix is a new forward migration, not routing around the guard.
  `uv run alembic revision -m '<what this corrects>'`.
- Money is `NUMERIC`/`DECIMAL` or integer minor units. Never `float`/`double precision` on any
  column that holds or derives an amount. Check this on every new or altered column, not just
  ones that look obviously financial.

## Structural checklist

- **Applies cleanly from an empty database.** `uv run alembic upgrade head` from scratch.
- **Applies cleanly from the previously released schema**, not just an empty one — a migration
  that only works from nothing can still break a real upgrade-in-place. CI's
  `migration-preflight` job checks this against the actual previously-published image on every
  push to `main`; don't rely on that alone, reason through it at authoring time too.
- **Additive first, destructive later** if the change needs to be safe while the old application
  version is still running during a rolling/staged deploy — add the new column/table nullable or
  defaulted, backfill, switch reads, only then drop/narrow in a later migration.
- **Drops, renames, and type narrowings are called out explicitly** with a written data-loss
  assessment in the migration's docstring or the PR description. No silent destructive change.
- **New foreign keys, unique constraints, and check constraints are backed by indexes** wherever
  they'll actually be queried — verify with `EXPLAIN` if the table isn't trivially small.
- **Plaid natural keys are unique-constrained**: `transaction_id`, `account_id`, `item_id` (or
  whichever the migration touches) must be unique-constrained so an idempotent re-sync cannot
  duplicate rows. This is the single most important property for anything under `plaid.*` — see
  `.opencode/skills/plaid-sync-review/SKILL.md` for the ingestion side of this same invariant.
- **Downgrade path**: either a real, tested `downgrade()`, or an explicit, documented reason the
  migration is one-way (e.g. a destructive drop that can't be un-dropped). Don't leave
  `downgrade()` as a silent no-op without saying why.
- **Grants**: if the migration touches roles or table ownership, update (or add) the
  role-boundary test that proves `finance_agent` still cannot `UPDATE`/`DELETE`/`INSERT` on
  `plaid.*` — `tests/security/`. A migration that silently widens a role's grants is exactly the
  kind of change this project's whole security model depends on catching.

## Before opening the PR

- Hand off to the `database` agent for review if this session isn't already running as it.
- Run `pre-merge-review` (`.opencode/skills/pre-merge-review/SKILL.md`) regardless — migrations are
  Class B, which already requires independent review, but the fresh-context correctness pass is
  additive to that, not a substitute for it.
- PR description states the risk class (almost always Class B for schema changes), the
  data-loss assessment (or "none" explicitly), and the downgrade story.
