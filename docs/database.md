# Database

Milestone 1 deliverable. PostgreSQL, SQLAlchemy 2.x models, Alembic migrations. See `docs/architecture.md` for how this fits the rest of the system and `docs/security-model.md` for the invariants this schema enforces.

## Schemas

| Schema | Owns | Written by |
|---|---|---|
| `plaid` | `items`, `accounts`, `transactions`, `sync_state` — raw source-of-truth facts | `finance_app` only (deterministic ingestion) |
| `user` | `transaction_category_overrides`, `transaction_tags`, `transaction_notes`, `preferences` — interpretation | `finance_app`, `finance_agent` |
| `finance` | `budgets` — modeled user constructs | `finance_app`, `finance_agent` |
| `agent` | `conversations`, `messages`, `analysis_runs`, `tool_calls`, `category_suggestions` — audit trail | `finance_app`, `finance_agent` |
| `ops` | `job_runs`, `sync_runs`, `errors`, `backup_runs`, `releases` — sanitized operational state | `finance_app` only |

`plaid.transactions` is never hard-deleted. A Plaid `removed` event sets `removed_at` instead — provenance is never destroyed (handoff §4.1). `finance.budgets` is deliberately not split into a separate `budget_categories` table: one budget is one category's monthly amount, matching how the CLI and the agent's `create_budget`/`update_budget` tools present it (handoff §7 — do not over-normalize without a demonstrated need).

## Roles

Created by `migrations/versions/0002_..._roles_and_grants.py`. `finance_migrator` is not created there — it is the cluster bootstrap role (the host PostgreSQL instance's own superuser-ish role in dev, per ADR-019 — no Docker, no container `POSTGRES_USER`; a real Postgres role with `CREATEROLE`/DDL rights in production) and is what migrations connect as. One host PostgreSQL instance serves both `finance_dev` and `finance_prod` as separate logical databases (ADR-019); this role table applies identically to each.

| Role | Grants | Used by |
|---|---|---|
| `finance_owner` | `ALL` on every schema | nothing in the normal application path |
| `finance_migrator` | DDL; creates the other five roles; plain membership in all five (migration `0007`, for `DROP OWNED BY` during downgrade) | `alembic upgrade` only |
| `finance_app` | `SELECT/INSERT/UPDATE/DELETE` on all five schemas | the deterministic application (ingestion, CLI, budgeting) |
| `finance_agent` | `SELECT` only on `plaid.*`; full DML on `user.*`/`finance.*`/`agent.*`; no grant on `ops.*` | the runtime conversational agent's tool layer |
| `finance_observer` | `SELECT` only on `ops.*`, plus (as of migration `0004`) `plaid.items`/`plaid.sync_state`/`public.alembic_version` | `finops` health/status commands |
| `finance_backup` | `SELECT` only, every schema, every sequence, plus `public.alembic_version` | `pg_dump` (`deploy/scripts/backup.sh`) |

`ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator` is set for every schema/role pair, so tables added by future migrations inherit the right grants automatically — a new migration doesn't need to touch `0002` or duplicate its grants.

Migration `0004` (Milestone 7) adds `ops.backup_runs`/`ops.releases` (inheriting `0002`'s default-privilege grants automatically) plus three grants outside that pattern, each documented in the migration's own docstring: `finance_backup`/`finance_observer` SELECT on `public.alembic_version` (Alembic's own bookkeeping table lives outside the five application schemas), `finance_observer` SELECT on `plaid.items`/`plaid.sync_state` (narrow, status-only — deliberately not `plaid.accounts`/`plaid.transactions`), and `finance_backup` SELECT on every sequence in every application schema (`pg_dump` reads a table's owning sequence and aborts the whole dump without it — a real failure hit while building the backup script, not a hypothetical one).

Migration `0007` (Milestone 7) grants `finance_migrator` plain membership in all five roles. `finance_migrator` has `CREATEROLE`, sufficient to create/alter/drop them, but Postgres's `DROP OWNED BY` command requires role membership (or superuser privilege) — not just creation privilege. `0002`'s downgrade loop runs `DROP OWNED BY` for all five roles, and would fail without this grant. The grant is one-way: `0007.downgrade()` is deliberately a no-op, not a symmetric revoke, because Alembic runs downgrades newest-first (so a revoke would execute before 0002's downgrade needs the membership), and because 0002's downgrade ends with `DROP ROLE IF EXISTS`, which destroys all associated membership grants automatically. See the migration's own docstring for the detailed reasoning.

Passwords resolve from `<ROLE>_DB_PASSWORD` environment variables, falling back to the `devpassword` literal already used for the host dev PostgreSQL instance (and CI's own Postgres service container). That default is synthetic and disposable, never a production credential — production role passwords are provisioned out of band as systemd encrypted credentials (`docs/security-model.md` invariant 4) and are never read from this repository.

## Connections

- `DATABASE_URL` — the application's own connection, as `finance_app`. `src/finance_app/db/session.py` reads this.
- `ALEMBIC_DATABASE_URL` — migrations only, as `finance_migrator`. `migrations/env.py` reads this, deliberately not `DATABASE_URL`, because `finance_app` has no DDL rights and migrations must not silently fall back to a role that happens to have more privilege than it needs.
- `AGENT_DATABASE_URL` — the runtime agent's tool layer, as `finance_agent`. `src/finance_app/agent/db.py`.
- `OBSERVER_DATABASE_URL` — `finops`'s read commands (`health`/`sync-status`/`db-status`/`migration-status`/`backup-status`/`recent-errors`), as `finance_observer`. `src/finance_app/ops/db.py`. `finops deploy`/`rollback` are the one exception: they write `ops.releases`, so they use `DATABASE_URL`/`finance_app` instead — see `src/finance_app/cli/finops.py`'s module docstring.
- `BACKUP_DATABASE_URL` — `pg_dump`, as `finance_backup`. `src/finance_app/ops/backup.py`. Recording the backup's own `ops.backup_runs` row still goes through `DATABASE_URL`/`finance_app`, since `finance_backup` cannot write anywhere.

## Verifying the boundary

```bash
# host PostgreSQL instance running, finance_dev database created (ADR-019 — no Docker)
uv run alembic upgrade head
uv run pytest tests/integration -v -m integration   # migrations + repositories
uv run pytest tests/security -v -m integration       # finance_agent cannot mutate plaid.*
```

`tests/security/test_role_grants.py` is the test the Milestone 1 exit criteria refers to: it connects as each role directly (not through the application) and asserts the grant boundary, not application-level convention. A Postgres `permission denied` error, not an application-level check, is what stops `finance_agent` from writing `plaid.*`.

## Synthetic fixtures

`tests/plaid_fixtures/synthetic.py` provides Plaid-shaped test data — no real financial data ever. `golden_month()` returns one representative month covering paycheck income, rent, groceries, a restaurant charge and its refund, an internal transfer, ATM cash, a subscription, and a pending transaction, matching the categories handoff §24 requires the eventual agent-eval golden dataset to cover.

## What Milestone 1 deliberately does not do yet

- No Plaid client or sync loop (Milestone 2) — `db/repositories/transactions.py` has `upsert`/`mark_removed` ready for it to call.
- No analytics layer (Milestone 3) reading these tables yet.
- No CLI commands (Milestone 4) wired to the repository yet.
- The runtime agent's own `finance_agent`-scoped connection and semantic tools (Milestone 5) don't exist yet; this milestone only proves the database-level boundary they will run inside.
