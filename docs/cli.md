# The `finance` CLI (Milestone 4)

`finance` exposes the Milestone 3 analytics layer (`src/finance_app/analytics/`) as Typer/Rich
commands. Every number it prints comes from the same deterministic query path the future
runtime agent will call — the CLI is not a second implementation of spending/income/cashflow
logic, it is a thin presentation layer over `src/finance_app/analytics/*.py`.

**Exit criteria this satisfies (handoff §29):** useful even with the runtime LLM unavailable —
none of these commands touch the runtime agent provider (OpenAI or Anthropic) or any model.

## Commands

| Command | Purpose |
| --- | --- |
| `finance status` | Application/database/sync health. Always exits 0, even with no database — see below. |
| `finance sync` | Run `/transactions/sync` to completion (Milestone 2). |
| `finance spending [--month YYYY-MM] [--category NAME]` | Spending by effective category for a period, plus total. |
| `finance income [--month YYYY-MM]` | Total income for a period. |
| `finance cashflow [--month YYYY-MM]` | Income, spending, net, and savings rate for a period. |
| `finance budget [--month YYYY-MM]` | Active budgets against actual spend for a period. |
| `finance transactions recent [--days N] [--limit N]` | Most recent transactions, newest first. |
| `finance transactions search QUERY [--days N] [--limit N]` | Case-insensitive substring match on name/merchant. |
| `finance chat` | Interactive conversation with the runtime financial agent (Milestone 5). Requires a reachable database and the active `AGENT_PROVIDER`'s API key — see [`docs/financial-agent.md`](financial-agent.md). |
| `finance version` | Print the application version. |

`--month` defaults to the current calendar month everywhere it appears; periods are the same
half-open `[start, end)` ranges `analytics/periods.py` uses, so a CLI figure and the underlying
analytics-function figure for the same period are always identical by construction.

## `status` degrades gracefully without a database

`finance status` is the one command that must stay useful when the rest of the stack isn't up
(e.g. the database isn't reachable yet) — it's the first thing to run when diagnosing
"why doesn't `finance` work." It always exits 0: on a database error it prints
`database unavailable: ...` and stops, rather than raising. Every other command requires a
reachable database and will exit non-zero on connection failure, since there is nothing useful
to report without one.

## `transactions` — same effective view as everything else

`finance transactions recent`/`search` are built on `analytics/transactions.py`, which in turn
calls the same `fetch_effective_transactions` every other analytics module uses (see
`analytics/_effective.py`). A transaction's category, transfer/income classification, and any
user override are identical whether you look at it via `finance transactions search`, `finance
spending`, or `finance budget` — there is exactly one query path that resolves "what is this
transaction," not one per command.

## Adding a new command

1. Put the logic in `src/finance_app/analytics/` (or reuse what's there) — never inline a
   Decimal computation or a raw SQL query in `cli/main.py`. The CLI's job is argument parsing
   and Rich rendering only.
2. Reuse `_resolve_period()` for any command that takes `--month`.
3. Add both a unit test (argument parsing, `status` without a database) and an integration test
   against the golden dataset (`tests/integration/test_cli.py`), matching the existing pattern.

## `finops` (Milestone 7) — the production operations interface

A second, separate entrypoint (`src/finance_app/cli/finops.py`) — not a subcommand of `finance`
— because its authority is different: `finance` is the user-facing app; `finops` is the narrow
diagnostic/deployment surface handoff §10 requires so autonomous engineering agents never need
`psql`/shell/SSH against production. See `docs/deployment.md` for the full design and ADR-019
for the bare-metal design this table now describes (**no Docker**, revised 2026-09-13 — supersedes
the Compose-based mechanism in the "Connects as" column below where it once said `docker compose`).

| Command | Purpose | Connects as |
| --- | --- | --- |
| `finops version [--json]` | App version + running release id | none (reads in-process version string + release id; no DB connection) |
| `finops health [--json]` | Aggregate database/migration/sync/backup health; exits non-zero if unhealthy | `finance_observer` |
| `finops sync-status [--json]` | Most recent Plaid sync run and cursor state | `finance_observer` |
| `finops db-status [--json]` | Database reachability | `finance_observer` |
| `finops migration-status [--json]` | Applied Alembic revision vs. repo head | `finance_observer` |
| `finops backup-status [--json]` | Most recent backup and whether it's restore-verified | `finance_observer` |
| `finops recent-errors [--limit N] [--json]` | Recent sanitized `ops.errors` rows | `finance_observer` |
| `finops restart` | Re-runs `finance selfcheck` against the release `/opt/finance/current` points at and reports whether it's still healthy. Never changes `current`. | `finance_observer` (read current release) |
| `finops deploy <sha>` | Confirm/copy the release directory, migrate, health-check, promote-or-auto-rollback (ADR-008's principle, ADR-019's mechanism) | `finance_app` (write) |
| `finops rollback` | Repoint `current` at the tracked previous release directory and restart | `finance_app` (write) |

Every read command connects as `finance_observer` — strictly read-only, never a financial
payload in the output (`src/finance_app/ops/db.py`). `deploy`/`rollback` are the only commands
that change production state, and they do so narrowly: a `current`-symlink repoint plus a
`systemctl restart` of the production units, plus a bookkeeping row (`src/finance_app/ops/release.py`,
conceptually unchanged from the Docker design — it only ever tracked release identifiers and
status, never anything Docker-specific). None of them accept a SQL string, a shell string, or an
arbitrary command — see `docs/deployment.md` and `docs/runbooks/deploy.md` for exactly how
`deploy`/`rollback`/`restart` operate in production. **As of 2026-09-13 this table describes the
target design (ADR-019); the current code still shells out to `docker compose` via
`src/finance_app/ops/compose.py` and has not been rewritten yet** — a follow-up session's work.
