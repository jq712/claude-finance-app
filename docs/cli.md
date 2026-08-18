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
(e.g. `docker compose` hasn't been started yet) — it's the first thing to run when diagnosing
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
