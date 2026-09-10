# 0. Repo identity

- **Repo path / project name:** `/Users/jrq712/projects-dev/claude-finance-app` — "finance-app" (single-user headless financial intelligence application).
- **Primary language(s) / runtime:** Python (`.python-version` present), 3.12 venv observed.
- **Package manager:** `uv` (`uv.lock`, `pyproject.toml`).
- **How to run locally:**
  ```
  uv sync
  docker compose -f deploy/compose.dev.yaml up -d
  uv run alembic upgrade head
  uv run pytest
  uv run ruff format . && uv run ruff check .
  uv run pyright
  ```
  Verified working: `uv run pytest --collect-only` succeeds (175 tests collected), `uv run pyright`/`ruff` run, a dev Postgres container (`finance-app-postgres-dev`, port 5433) was already running and `alembic current` reports head `b2518380bc18`. The app **does run** — CLI (`finance`/`finops` Typer apps) imports cleanly and unit tests pass. There is no built container image, systemd unit, or production compose file, so "running in production" is not runnable/verifiable — see §3/§5.
- **Last 10 commits:**
  ```
  bec4805 Milestone 6: agent eval framework (ADR/handoff §24) (#9)
  a398a9e Milestone 5: conversational financial agent (ADR-014) (#8)
  750ac67 Adopt provider-interchangeable runtime agent for Milestone 5 (ADR-014) (#7)
  c5ae9bd Milestone 4: deterministic CLI (#6)
  1da67f6 Milestone 3: deterministic analytics (#5)
  22e5655 Milestone 2: Plaid Transactions Sync ingestion (#4)
  4319710 Update README status to Milestone 1 complete (#3)
  9582b48 Milestone 1: database foundation — schema, models, roles, grants (#2)
  b333a96 Complete Milestone 0: Python project, dev Postgres, CI (#1)
  0104d46 Bootstrap engineering rails for the finance app
  ```
  One PR-shaped commit per milestone, 0 through 6, exact dates not re-verified.
- **Current git status:** dirty. Branch `milestone-7-production-deployment`. Modified: `src/finance_app/config/settings.py`, `src/finance_app/db/models/ops.py`. Untracked: `migrations/versions/0004_b2518380bc18_backup_and_release_tracking.py`, `src/finance_app/ops/logging.py`, `src/finance_app/ops/status.py`. This is mid-flight Milestone 7 work, uncommitted.
- **Test command / status:**
  - `uv run pytest --collect-only -q` → **175 tests collected, 0 errors**.
  - `uv run pytest tests/unit -q` → **33 passed**.
  - `uv run pytest tests/integration tests/security -q -m integration` (against live dev Postgres) → **129 passed, 1 failed**. Failure: `tests/integration/test_migrations.py::test_alembic_is_at_a_known_head` hardcodes the pre-Milestone-7 head revision (`715521e125d4`) and now sees `b2518380bc18` (new uncommitted migration). Expected WIP breakage, but means the working tree does not currently pass its own suite.
  - `tests/agent_evals` (12 cases) not run — requires `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`; CI skips it under the same condition (`tests/agent_evals/conftest.py`).
  - `uv run pyright` → **2 errors**, both in the new untracked `src/finance_app/ops/status.py:162-163` (`reportOptionalMemberAccess` on `latest_verification.finished_at`, accessed without narrowing `X | None`). Type-checking is **not clean** on the current tree, violating this project's own "Definition of done."
  - `uv run ruff check .` → clean. `uv run ruff format --check .` → clean (127 files).

# 1. Intended end goal

**Source of this spec:** WRITTEN SPEC — `CLAUDE_FINANCE_APP_HANDOFF.md` (1807 lines) plus `CLAUDE.md` and `README.md`. Unusually complete and pre-dates the code; the code visibly tracks it milestone-by-milestone.

- **One-sentence product statement:** A secure, single-user, headless financial intelligence app that syncs one Plaid Item into PostgreSQL, computes deterministic financial analytics, and exposes both a scriptable CLI and a tool-using conversational agent (OpenAI- or Claude-API–backed) that can explain but never fabricate numbers.
- **Target user:** A single individual (the repo owner) running this on their own Linux VPS over SSH — not multi-tenant SaaS.
- **Core user journeys:**
  1. Link one Plaid Item once (bank checking account) — `docs/runbooks/plaid-link.md`.
  2. Daily (systemd timer, planned) or on-demand (`finance sync`) incremental transaction sync via Plaid's `/transactions/sync`.
  3. Run deterministic CLI commands (`finance status`, `spending`, `income`, `cashflow`, `budget`, `transactions recent/search`) with no LLM involved.
  4. `finance chat` — LLM agent calls semantic tools to answer questions and make controlled writes (categorize, tag, note, budget).
  5. Operate/maintain the deployed system via a narrow `finops` CLI without giving engineering agents raw DB/shell access to production.
- **In-scope features (handoff milestones 0–9):** bootstrap; Postgres schema + roles/grants; Plaid sync ingestion; deterministic analytics; CLI; provider-interchangeable conversational agent with semantic tools; agent eval framework; production deployment (Docker/Compose, systemd, backups, CI/CD, rollback); Plaid webhook; autonomous maintenance loop.
- **Explicitly out of scope:** Kubernetes, Redis/Celery/Kafka, microservices, multi-tenancy, proactive push notifications, a public web UI/dashboard, raw SQL/shell access for the LLM, floats for money.
- **Success criteria for "v1 done" (handoff §29):** migrations apply cleanly from empty DB; sync survives interruption/duplication without data loss; golden dataset analytics return exact values; CLI usable with LLM disabled; agent answers traceable to deterministic results with no boundary violations; evals automated per-provider; release/rollback repeatable; backup-restore verified; production runs without engineering environment holding runtime secrets.
- **Constraints:** Python + PostgreSQL + SQLAlchemy 2.x/psycopg + Alembic; Typer/Rich CLI; Docker Compose (no k8s); single Linux VPS; systemd timers; Plaid Sandbox only in dev; OpenAI or Anthropic as runtime provider (config-switchable); Claude Code as separate engineering agent.
- **Confidence:** high.
- **Ambiguities:** (a) whether Plaid webhooks (M8) are truly needed given daily sync is deemed sufficient; (b) whether the Postgres-GRANT-level agent boundary is considered sufficient security vs. wanting an additional prompt-injection-specific gate beyond the 3 eval adversarial prompts; (c) how autonomously Milestone 9's maintenance loop is meant to run — described in detail but zero code exists, two milestones past current branch.

# 2. Current implementation status

| Feature | Status | Evidence (paths) | Notes / gaps |
|---|---|---|---|
| Repo bootstrap / CLAUDE.md / ADRs / CI skeleton (M0) | DONE | `CLAUDE.md`, `docs/adr/ADR-001..014`, `.github/workflows/ci.yml` | 14 ADRs match handoff §28's list exactly |
| DB schema, models, roles/grants (M1) | DONE | `src/finance_app/db/models/{plaid,user,finance,agent,ops}.py`, `migrations/versions/0001_*`, `0002_*`, `tests/security/test_role_grants.py` (21 tests) | Role-grant boundary enforced at Postgres GRANT level and live-tested |
| Plaid Transactions Sync ingestion (M2) | DONE | `src/finance_app/plaid/sync.py` (374 lines), `client.py`, `tests/integration/test_plaid_sync.py` (12), `tests/unit/test_plaid_sync_retry.py` (5) | Cursor-based, per-page commit, advisory lock, retry, tombstone-not-delete |
| Deterministic analytics (M3) | DONE | `src/finance_app/analytics/{spending,income,cashflow,recurring,budgeting,categories,periods,transactions,_effective}.py`, `tests/integration/test_analytics*.py` (35 tests) | `Decimal` return types observed, not floats |
| Deterministic CLI (M4) | DONE | `src/finance_app/cli/main.py` (309 lines): `status`, `sync`, `spending`, `income`, `cashflow`, `budget`, `transactions recent/search`, `version`, `chat` | Matches handoff §9 target list |
| Conversational agent, provider-interchangeable (M5) | DONE | `src/finance_app/agent/{loop.py,conversation.py,db.py,prompts.py}`, `agent/providers/{base,openai,anthropic,factory}.py`, `agent/tools/{read.py,write.py}` (19 tools: 10 read, 9 write), `tests/security/test_agent_db_boundary.py` | No `run_sql`; `agent/db.py` binds to `finance_agent` role, live-verified (`DELETE FROM plaid.transactions` raises `permission denied`) |
| Agent eval framework (M6) | DONE (as framework) / UNVERIFIED (as run) | `evals/cases.py` (12 cases), `evals/fixtures/golden_dataset.py` (312 lines), `tests/agent_evals/*` | Includes real prompt-injection cases; not executed in this audit (needs live API key) |
| Production deployment (M7 — in progress) | PARTIAL/STUB | `config/settings.py` (uncommitted diff), `db/models/ops.py` (uncommitted diff: `BackupRun`, `Release`), `migrations/versions/0004_*` (untracked), `ops/{logging.py,status.py}` (untracked) | Data model + status logic exist and are well-designed; **nothing wires it up** — `finops` CLI has only `version` (19 lines); zero call sites for `ops.status`/`ops.logging` anywhere; no Dockerfile, no prod compose, no systemd units, no `docs/deployment.md`/`docs/backups.md`; no CI deploy job; `ADR-015` cited three times in new code but does not exist; `ops/health.py` is a 1-line stub |
| Plaid webhook (M8) | MISSING | — | No webhook route/signature validation. Correctly deferred. |
| Autonomous maintenance loop (M9) | MISSING | — | No code. Expected — two milestones ahead. |

**What a user can actually do today, start to finish:** With Plaid Sandbox credentials and a running dev Postgres: `uv sync` → bring up dev DB → `alembic upgrade head` → `finance sync` pulls sandbox transactions → `finance spending`/`income`/`cashflow`/`budget`/`transactions recent|search` give deterministic answers → `finance chat` for a tool-using session against OpenAI or Anthropic. This is a real, working end-to-end CLI app for a single Plaid Sandbox item. What a user **cannot** do: deploy to a VPS via any documented/scripted path, check operational health via `finops` beyond a version string, or get webhook-driven near-real-time sync.

**What looks implemented but is fake/orphaned:** `ops/status.py` (269 lines) and `ops/logging.py` (114 lines) are fully-written, careful modules — but dead code today; nothing imports them, and `cli/finops.py` never calls them. Reads as "built ahead of wiring," not fake, but a skim of file existence would overcredit `finops` maturity. `ops/health.py` is a pure docstring stub.

**Percent-complete estimate for v1 (through Milestone 7): ~78–82%.** Milestones 0–6 are solidly done with live-tested evidence (129/130 integration+security tests pass, the one failure being stale WIP). Milestone 7 is ~25–30% done: the hard conceptual parts (schema, sanitized logging, status-aggregation logic) exist, but the operationally load-bearing parts (Dockerfile, systemd, prod compose, `finops` CLI surface, CI deploy job, ADR-015, ops docs) are entirely absent.

**Biggest 5 holes blocking a usable v1:**
1. No Dockerfile / production Compose / systemd units — the app cannot be deployed per the documented target architecture.
2. `finops` CLI is a stub (1 of ~10 target commands); backend functions exist but are unwired.
3. No CI/CD deploy pipeline — CI only lints/tests/evals/scans.
4. Missing `ADR-015` despite being cited by name in pending code comments.
5. `pyright` is not clean on the current tree (2 errors), and one integration test now fails — the tree, as-is, does not meet this project's own Definition of Done.

# 3. Architecture

- **High-level shape:** Python modular monolith, single deployable process, single Postgres database with logically separated schemas (`plaid`, `user`, `finance`, `agent`, `ops`), each with its own least-privilege DB role.
- **Directory map:**
  ```
  src/finance_app/
    agent/        conversational agent: provider-agnostic loop, OpenAI/Anthropic adapters, semantic tools (read/write), audited DB session
    analytics/    deterministic spending/income/cashflow/recurring/budgeting math over "effective" (Plaid+override-merged) transactions
    cli/          Typer apps: `finance` (main.py, 309 lines) and `finops` (19 lines, stub)
    config/       pydantic-settings Settings (DB URLs, Plaid, agent provider keys, backup/release settings)
    db/           SQLAlchemy models (5 schema modules), repositories, session/engine setup
    ops/          logging.py (sanitized JSON logging), status.py (finops backend logic, unwired), health.py (stub)
    plaid/        client.py (thin SDK wrapper), sync.py (374-line incremental sync engine)
  migrations/     Alembic, 4 revisions (schema → roles/grants → agent provider column → backup/release tables)
  tests/          unit / integration / security / agent_evals / plaid_fixtures — 175 tests total
  evals/          golden synthetic dataset + 12 eval cases (incl. 3 adversarial/injection prompts)
  docs/           architecture, security-model, database, plaid-sync, analytics, cli, financial-agent, adr/ (14), runbooks/ (2)
  .claude/        agents/ (7 subagent defs), hooks/, settings.json
  deploy/         compose.dev.yaml only — no prod compose, no Dockerfile, no systemd/
  ```
- **Data model / schema (5 logical schemas):**
  - `plaid.*`: `items`, `accounts`, `transactions`, `sync_state` — source-of-truth, immutable except by ingestion code.
  - `user.*`: `transaction_category_overrides`, `transaction_tags`, `transaction_notes`, `preferences` — interpretation layer.
  - `finance.*`: `budgets`.
  - `agent.*`: `conversations`, `messages`, `analysis_runs`, `tool_calls` (has `provider` column, migration 0003), `category_suggestions`.
  - `ops.*`: `job_runs`, `sync_runs`, `errors`, plus new (uncommitted) `backup_runs`, `releases`.
  - Money handled via `Decimal` return types in analytics; consistent with the project's no-float rule.
- **State/data flow:** `Plaid fact (plaid.*) + override (user.*) → analytics/_effective.py fetch_effective_transactions() → analytics/*.py compute → CLI or agent tool → response`. Agent writes land only in `user.*`/`finance.*`/`agent.*`.
- **Auth / permissions:** No end-user auth (single SSH-accessed user by design). 6 DB roles (`finance_owner`, `finance_migrator`, `finance_app`, `finance_agent`, `finance_observer`, `finance_backup`) enforced by GRANTs (migration 0002), tested live in `tests/security/test_role_grants.py` (21 tests) and `test_agent_db_boundary.py`.
- **API surface:** No HTTP API. Two CLIs (`finance`, `finops`) plus outbound calls to Plaid, OpenAI, Anthropic.
- **Third-party services:** PostgreSQL 17, Plaid (Sandbox), OpenAI SDK, Anthropic SDK.
- **Key design decisions (ADRs, all "Accepted"):** Postgres as sole store (001); modular monolith (002); Plaid Sync over polling (003); raw rows immutable to LLM (004); no arbitrary agent SQL (005); deterministic arithmetic (006); Git/CI as only prod path (007); immutable releases + rollback (008); isolated dev/Sandbox (009); prod secrets excluded from eng env (010); systemd timers (011); daily sync fallback even with webhooks (012); Claude Code vs. runtime provider split (013, superseded); provider-interchangeable agent (014).
- **Coupling / duplication / god-files:** None observed. Largest files: `plaid/sync.py` (374 lines), `agent/tools/read.py` (374 lines) — both cohesive. Provider adapters are comparable in size (`openai.py` 112 lines, `anthropic.py` 106 lines), suggesting parallel not divergent implementations.
- **What would be hard to change later:** The 5-schema split and 6-role privilege model are load-bearing for every security test and the "agent cannot mutate facts" guarantee. The provider abstraction is comparatively cheap to extend.

# 4. Engineering quality

- **Structure/modularity: 5** — clean separation, matches handoff layout.
- **Naming/readability: 5** — self-documenting names, comments explain *why* (`plaid/sync.py:19-28`).
- **Type safety/validation: 4** — SQLAlchemy 2.x `Mapped[...]`, dedicated `agent/tools/_validation.py`, pydantic-settings; docked for the 2 current pyright errors.
- **Error handling: 4** — sync distinguishes transient/non-retryable/transport errors with dedicated tests; agent loop tested against malformed tool calls/unknown tools.
- **Security basics: 5** — no secrets found; `.gitignore` excludes `.env*`; parameterized queries throughout observed code; DB-role boundary enforced by Postgres itself and live-tested; dedicated log-sanitization module.
- **Testing: 5** — 175 tests, real Postgres (not mocked), adversarial test files by name, eval suite includes literal prompt-injection strings. 129/130 non-eval DB tests pass.
- **Docs: 5** — README, 1807-line handoff, 8 topic docs, 14 ADRs, 2 runbooks. Gap: `docs/deployment.md`/`docs/backups.md` required by handoff §27 but absent.
- **Observability: 3** — `ops/logging.py`/`ops/status.py` well-designed but currently dead code; no live structured logging observed wired into `cli/main.py`.
- **DX: 5** — `uv sync`/`ruff`/`pyright` all run clean (except 2 WIP pyright errors); thorough `.env.example`; CI mirrors local commands.
- **Git hygiene: 4** — one commit per milestone, descriptive; current WIP branch not test-clean.

**Concrete strengths:**
1. DB role/privilege boundary is Postgres-GRANT-enforced and live-tested (`tests/security/test_agent_db_boundary.py`).
2. 19 agent tools, zero `run_sql`/shell/filesystem surface (grep-confirmed).
3. Eval dataset includes real adversarial/injection prompts (`evals/cases.py:269,275,281`).
4. Sync engine documents/tests cursor regression, page-retry-from-committed-cursor, tombstone-not-delete.
5. Money as `Decimal`, not float.
6. Tool definitions genuinely single-defined and adapter-translated, not hand-duplicated.
7. ADRs actually cited by file path in code comments — documentation-as-memory is real, not aspirational.

**Concrete defects:**
1. `src/finance_app/ops/status.py:162-163` — pyright `reportOptionalMemberAccess`, `latest_verification: BackupRun | None` accessed unguarded.
2. `ops/status.py`, `ops/logging.py` — written but imported nowhere; dead code as of this snapshot.
3. `src/finance_app/cli/finops.py` — only `version` implemented.
4. `tests/integration/test_migrations.py:44` — hardcodes stale head `715521e125d4`, now fails.
5. `ops/status.py:41` and migration `0004` docstring cite `ADR-015`, which doesn't exist.
6. `docs/deployment.md`, `docs/backups.md` — required by handoff §27, absent.
7. No `Dockerfile`, no prod `deploy/compose.yaml`, no `deploy/systemd/*`.
8. `src/finance_app/ops/health.py` — single-line docstring stub.
9. `.github/workflows/ci.yml` — no build/deploy job.
10. Repo's own Definition of Done ("types/lint clean") currently violated by uncommitted state.

**TODO/FIXME/HACK count:** 0 found via grep across `src/ tests/ docs/ migrations/ evals/`.

**Dead code/duplication/commented-out blocks:** Only `ops/status.py`/`ops/logging.py` (unwired WIP, not forgotten cruft). No commented-out blocks or duplicated tool-definition logic observed.

**Secrets/credentials:** None found. `.env.example` uses placeholders only; `.gitignore` correctly excludes `.env*`; CI runs a gitleaks secret-scan job.

# 5. Correctness and runtime reality

- **Can the app boot?** Verified, not inferred: `pytest --collect-only` imports the whole tree cleanly; `alembic current` against live dev Postgres returns head `b2518380bc18`; `pytest tests/integration tests/security -m integration` actually executed 130 tests against a real database, 129 passing.
- **Known broken paths:** `finops` beyond `version`; working tree fails pyright (2 errors) and one integration test (stale hash) — both uncommitted-WIP artifacts, not committed regressions.
- **Happy-path vs. edge-case:** Demonstrably tested — `test_analytics_adversarial.py` (10), `test_transaction_repository_adversarial.py` (12), Plaid retry tests (5), agent-loop malformed-input tests (8), all collected successfully.
- **Data persistence:** Real PostgreSQL via SQLAlchemy/Alembic, confirmed against a live container with real GRANT-based roles.
- **Empty state/bad input/restart:** `sync_status()`/`backup_status()` explicitly handle "never run"; CLI unit tests check rejection of negative days/malformed month strings; sync interruption/restart correctness is the entire design point of the cursor logic and is tested (12 tests, not individually re-read).
- **Test coverage vs. gaps:** Covers ingestion lifecycle, analytics (35 tests), DB role/permission boundaries (23 tests), CLI validation, agent-loop robustness, and (pending API keys) golden-dataset evals with 3 adversarial prompts. Misses: anything in the Milestone 7 surface — no tests exist for backup execution, restore verification, release/rollback CLI, or `ops/status.py`'s own 269 lines of logic.

# 6. Agent working style visible in the repo

- **Incremental, not rewrite-heavy:** One commit per milestone (0–6, PRs #1–#9), each building on the prior schema/module set; no abandoned directories or restarted architectures.
- **Spec-driven, not improvising:** The 1807-line handoff predates and is treated as authoritative; ADRs follow its numbered list; code cites them by number.
- **Test-first-leaning:** Adversarial/boundary tests exist for every risk class the handoff calls out (sync interruption, retry, role grants, malformed tool calls) — suggests tests were written to the handoff's explicit test list.
- **Consistency across files:** High — parallel provider adapter structure/size, uniform DB model pattern, every migration has a substantive rationale docstring.
- **Planning artifacts vs. code-only:** Extensive — ADRs, runbooks, per-topic docs, and the handoff itself are all substantive, not placeholders.
- **Signs of thrash:** None in committed history. One process-ordering slip: `ADR-015` is cited by new code before the ADR document itself was authored — minor, not evidence of architectural flip-flopping.

# 7. Honest verdict on THIS repo only

**Quality:** Unusually disciplined for its stage — clean modular structure, real tests against a real database, a documented and *actually enforced* (Postgres-GRANT-level, live-tested) security boundary between the LLM agent and source-of-truth financial data, and documentation that stays current with and is cited by the code. The one blemish is confined to uncommitted Milestone 7 WIP: a 2-error pyright regression and one stale hardcoded test assertion, both trivially fixable.

**Completeness:** Milestones 0–6 (foundation, ingestion, analytics, CLI, conversational agent, evals) are substantively complete and independently verified as working. Milestone 7 is early-stage: data model and status logic exist, but none of the deployment mechanics (Docker image, systemd, prod compose, CI/CD deploy pipeline, `finops` CLI surface, ADR-015, deployment/backup docs) are present. Milestones 8–9 haven't started, as expected.

**If development stopped today:** Everything through Milestone 6 is salvageable as-is. Milestone 7's WIP (models, settings, logging, status functions) is also salvageable — needs wiring, a Dockerfile/compose/systemd layer, and the missing ADR/docs. Nothing needs a rewrite.

**Next 5 steps if work resumed:**
1. Fix the 2 pyright errors in `ops/status.py:162-163` and update `tests/integration/test_migrations.py:44` to head `b2518380bc18`.
2. Write `docs/adr/ADR-015-*.md` since it's already cited by name.
3. Wire `cli/finops.py` to the existing `ops/status.py` functions.
4. Add a Dockerfile, `deploy/compose.yaml` (prod), and `deploy/systemd/*` units.
5. Extend `.github/workflows/ci.yml` with an image-build/deploy job; write `docs/deployment.md`/`docs/backups.md`.

**What a judge might overrate if skimmed:** `ops/status.py` (269 lines) and `ops/logging.py` (114 lines) look like mature ops tooling — they're unwired. Seeing 4 migrations and `BackupRun`/`Release` tables might suggest backups/releases are functional; only the schema exists, no backup/restore/deploy code runs against it.

**What a judge might underrate if skimmed:** The depth of security-boundary testing (Postgres-GRANT-enforced, live-verified) — `test_role_grants.py` sounds routine but is 21 tests directly proving the project's core safety invariant. Likewise the eval suite's literal prompt-injection strings (`evals/cases.py:269-287`) is a substantive signal a filename skim (`test_golden_evals.py`) would miss.

# 8. File appendix

**spec / docs**
- `CLAUDE_FINANCE_APP_HANDOFF.md` — authoritative 1807-line product+architecture+milestone brief.
- `CLAUDE.md` — condensed always-loaded rules.
- `docs/security-model.md` — trust boundaries/threat model.
- `docs/adr/ADR-014-provider-interchangeable-runtime-agent.md` — the design this milestone's agent architecture implements.
- `docs/architecture.md` — system shape/module boundaries.

**entrypoints**
- `src/finance_app/cli/main.py` — the `finance` CLI, 309 lines.
- `src/finance_app/cli/finops.py` — the `finops` CLI, currently a 19-line stub.

**core domain**
- `src/finance_app/plaid/sync.py` — 374-line incremental sync engine; highest-stakes correctness code.
- `src/finance_app/agent/loop.py` — provider-agnostic tool-execution loop; enforces write-tool boundaries and audit logging.
- `src/finance_app/agent/tools/read.py` / `write.py` — the entire semantic tool surface (19 tools).
- `src/finance_app/analytics/_effective.py` — where Plaid fact + user override merge into the "effective" view.
- `src/finance_app/db/models/ops.py` — shows both finished (M1-6) and in-progress (M7, uncommitted) schema.

**UI**
- N/A — no UI beyond the two CLIs.

**data / API**
- `migrations/versions/0002_fc8bd0e714f9_roles_and_grants.py` — the least-privilege role/GRANT model everything's security claims depend on.
- `migrations/versions/0004_b2518380bc18_backup_and_release_tracking.py` — untracked M7 migration.
- `src/finance_app/config/settings.py` — env-driven settings including uncommitted backup/release additions.

**tests**
- `tests/security/test_agent_db_boundary.py` — proves the agent literally cannot write to `plaid.*` at the DB level.
- `tests/security/test_role_grants.py` — 21 tests covering every role's actual Postgres privileges.
- `tests/integration/test_plaid_sync.py` — 12 tests covering added/modified/removed/pagination/interruption.
- `evals/cases.py` — 12 golden agent-eval prompts, including 3 adversarial/injection attempts.
- `tests/integration/test_migrations.py` — currently the one failing test (stale hardcoded head).

**config**
- `.env.example` — well-commented placeholder env.
- `.github/workflows/ci.yml` — 6 jobs (lint/typecheck, unit, integration, security, agent-evals, secret-scan); no deploy job.

# 9. Feature matrix CSV

```csv
feature,status,evidence_paths,notes
repo bootstrap and CLAUDE.md/ADRs,DONE,"CLAUDE.md;docs/adr/ADR-001..014;.github/workflows/ci.yml","14 ADRs match handoff list exactly"
database schema and roles/grants,DONE,"src/finance_app/db/models/*.py;migrations/versions/0001_*;0002_*;tests/security/test_role_grants.py","21 live tests against real Postgres"
plaid transactions sync,DONE,"src/finance_app/plaid/sync.py;tests/integration/test_plaid_sync.py;tests/unit/test_plaid_sync_retry.py","cursor-based, per-page commit, advisory lock, retry"
deterministic analytics,DONE,"src/finance_app/analytics/*.py;tests/integration/test_analytics*.py","Decimal-based, 35 tests"
deterministic CLI (finance),DONE,"src/finance_app/cli/main.py","status/sync/spending/income/cashflow/budget/transactions/chat"
conversational agent (dual provider),DONE,"src/finance_app/agent/**;tests/security/test_agent_db_boundary.py;tests/unit/test_agent_providers.py","19 tools, no run_sql, live-verified DB role isolation"
agent eval framework,DONE,"evals/cases.py;evals/fixtures/golden_dataset.py;tests/agent_evals/*","12 cases incl. 3 adversarial/injection prompts; not executed in this audit (needs API key)"
production deployment (Docker/systemd/CI-CD),PARTIAL,"src/finance_app/ops/{status.py,logging.py,health.py};src/finance_app/db/models/ops.py (uncommitted);migrations/versions/0004_* (untracked)","backend logic exists, unwired; no Dockerfile/compose.prod/systemd/CI-deploy; ADR-015 cited but missing; docs/deployment.md and docs/backups.md missing"
finops operations CLI,STUB,"src/finance_app/cli/finops.py","only `version` implemented; ~9 target commands missing despite backend existing in ops/status.py"
plaid webhook,MISSING,"-","not started; correctly deferred per plan (Milestone 8)"
autonomous maintenance loop,MISSING,"-","not started; correctly deferred per plan (Milestone 9)"
working-tree type/test cleanliness,DEFECT,"src/finance_app/ops/status.py:162-163;tests/integration/test_migrations.py:44","2 pyright errors + 1 stale hardcoded test assertion on current uncommitted WIP"
```
