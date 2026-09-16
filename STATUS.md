# STATUS.md — current-state snapshot

Snapshot of `origin/main` @ `ba5ec29` (2026-09-14, merge of PR #21), written 2026-09-15 from
this branch (`status-and-opencode`, itself `origin/main` with no divergence). Every claim below
was observed directly — a command run, a file read, `gh`/`git` queried — not carried over from
`README.md`, the handoff, or memory. See each line's citation.

Uncommitted in this working tree, unrelated to app state: a `.gitignore` `qa-pg/` line and a stray
`tmux-client-13198.log`. (`AGENTS.md`, `opencode.json`, and `.opencode/` are tracked on `main` now,
not uncommitted.)

## Last session

**2026-09-16 — `workflow-rules` (2 commits over `79773a3`; owner-merge by path)**

- Branch `workflow-rules` off `origin/main` (`79773a3`); cherry-picked `959dbac` (the post-#23
  STATUS.md record) so it survives on a branch.
- `AGENTS.md` gained the unattended-session rules: boot-from-git stale-branch check; worktree
  recovery only via `git show`/`git cherry-pick`; harness-config caching (restart OpenCode after
  editing `guards.js` or a `model:` pin; guards fail closed; no stub hooks on disk); reviewer
  integrity (only a subagent's real output is a review, a failed dispatch is recorded and the
  session stops); the Kimi specialist gate is Class B-only; stop cleanly when blocked. The changed
  Git bullets are mirrored into `CLAUDE.md`.
- Merge paths are now explicit: Class A via `merge-class-a.sh`; Class B (or any PR the script
  refuses on a reserved path) is owner-merged with `gh pr merge` after green CI.
- `merge-class-a.sh`'s `sensitive_pattern` now also reserves `AGENTS.md`; no other script behavior
  changed.
- **This change is owner-merge by path** (`AGENTS.md`/`CLAUDE.md`). The agent stops at the PR — it
  is not merged from this session. (`.opencode/` is *not* in the script's path screen today, so a
  harness-only PR would still pass it; tracked as a follow-up, not fixed here.)
- Still true from the prior session: `main` includes PR #23 (`79773a3`); QA-17 (upgrade-in-place)
  deferred in `docs/ADR-019`, required before first prod deploy; security findings 3–5 open
  (`selfcheck` identity assert; backup/restore + systemd reference Compose; stale comments).
- Next engineering: security findings 3–5, then ADR-019 leftovers (bare-metal systemd units +
  `Settings.production_units`), then owner-performed `/opt/finance` provisioning.
- Do not touch PR #15, `/opt/finance`, or `finance_ci_preflight` in an agent session.

## 1. What works now

Milestones 0–6 are merged (`gh pr list --state merged`: PRs #1–#9) and pass. Commands below run
against the host PostgreSQL 17.11 cluster on `127.0.0.1:5433` (already at this branch's alembic
head — nothing was started for this snapshot).

| Milestone | Demonstrated by |
|---|---|
| M1 — database foundation | `uv run alembic upgrade head`; `uv run pytest tests/integration/test_migrations.py -m integration` |
| M2 — Plaid sync (`src/finance_app/plaid/sync.py`, fake `SyncClient`, no network) | `uv run pytest tests/integration/test_plaid_sync.py -m integration` |
| M3 — deterministic analytics (`src/finance_app/analytics/`, `Decimal` only) | `uv run pytest tests/integration/test_analytics.py tests/integration/test_analytics_adversarial.py -m integration` |
| M4 — `finance` CLI (`src/finance_app/cli/main.py`) | `uv run finance --help`; `uv run pytest tests/unit/test_cli.py` |
| M5 — runtime agent, 10 read + 9 write semantic tools (`src/finance_app/agent/tools/`) | `uv run pytest tests/integration/test_agent_tools.py tests/security/test_role_grants.py -m integration` |
| M6 — eval harness, 12 cases (`evals/cases.py`) | `uv run pytest tests/agent_evals -m agent_eval` (skips without `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`) |
| M7 — shipped pieces: `ops/host.py`, `ops/identity.py`, `deploy/scripts/release.sh` | `uv run pytest tests/unit/test_ops_host.py tests/unit/test_release_script.py` |

**Whole-suite run, actually executed for this snapshot** (`uv run pytest -q`, PostgreSQL 17.11
already up on 5433, no containers started):

```
413 passed, 3 failed, 13 skipped, 2 xfailed in 50.92s
```

- The 2 `xfail` are the two known-open defects (§4.6 below) — expected, not a regression.
- 12 of the 13 skips are the agent-eval cases (no provider API key in this environment); 1 is
  `tests/unit/test_release_script.py:867`, conditional on running as root.
- The 3 failures are **not a code defect** — see §4.7.
- `uv run ruff format --check .` → `188 files already formatted`; `uv run ruff check .` → `All
  checks passed!`; `uv run pyright` → `0 errors, 0 warnings, 0 informations`.

## 2. What is partial — Milestone 7

Straight from `docs/adr/ADR-019-bare-metal-no-docker-deployment.md` lines 9–20 (its own
implementation-status table, re-verified against the file's current text for this snapshot):

| Piece | Status |
|---|---|
| Host PostgreSQL instance (`finance_dev`/`finance_prod`) | **Shipped** — PostgreSQL 17 (PGDG) on 5433; `finance_dev` at head. `finance_prod` not yet created. |
| `/opt/finance`, `finance-prod` Unix user, release-directory layout | **Not shipped** — owner-performed. |
| Release-copy script | **Shipped** — `deploy/scripts/release.sh`. |
| `finops deploy`/`rollback`/`restart` for the symlink/systemd model | **Shipped** — `src/finance_app/ops/host.py`, `src/finance_app/cli/finops.py`. `systemctl` restart is skipped with a warning until `Settings.production_units` is configured (needs the unit-file PR). |
| Non-forgeable release identity | **Shipped** — `src/finance_app/ops/identity.py`. |
| Explicit prod-opt-in (dev DSN default) | **Shipped** — `src/finance_app/config/env.py`. |
| `ops.releases.image_ref` → `artifact_ref` | **Shipped** — migration 0006. |
| Docker artifact removal (`compose.yaml`, `compose.dev.yaml`, `Dockerfile`, `ops/compose.py`) | **Partially shipped** — `ops/compose.py` deleted; the three other files are still present and orphaned, with ~35 tests still exercising them. |
| CI's Docker-shaped jobs redesigned | **Not shipped**. |
| systemd units rewritten for a venv binary | **Not shipped** — `ops/host.py:run_systemctl` and `Settings.production_units` are ready, waiting on the unit files. |

Other documented, still-open behavioral limitations (each source re-read for this snapshot):

- `docs/plaid-sync.md:108-134` — an unseen-id `removed` event is a silent no-op tombstone; a
  process killed between the last page commit and `record_success`/`finish_success` leaves
  `sync_state.status`/`sync_runs.status` stuck at `"running"` (observability gap, not corruption).
- `docs/analytics.md:123-152` — category-label drift across periods silently zero-fills the
  earlier label; a stale override silently re-attaches to a resurrected transaction.
- `docs/security-model.md:59` — secrets still cross into container environment variables for any
  service's lifetime; tracked as a follow-up, not yet closed.

## 3. What is not started

- **Milestone 8 — Plaid webhook** (handoff §29 "Milestone 8", design in §6.4, policy in ADR-012).
  No branch, no PR. `deploy/caddy/Caddyfile` is marked `# INACTIVE — Milestone 8 scaffold only.`;
  `PLAID_WEBHOOK_SECRET` is empty in `.env.example`; `docs/runbooks/webhook.md` does not exist.
- **Milestone 9 — autonomous maintenance** (handoff §29 / design §26). No branch, no PR.
- **Production itself.** Verified directly on the 5433 cluster: `finance_prod` does not exist.
  `/opt/finance` and the `finance-prod` Unix user do not exist (ADR-019 row 2). No deployment is
  running anywhere.

## 4. Known bugs / blockers

Ordered by how much work each one blocks.

### 4.1 This workspace was 28 commits behind `origin/main`

Before this snapshot, the checked-out branch (`opencode-migration`) was 28 commits behind
`origin/main` and 1 ahead (`git rev-list --left-right --count origin/main...opencode-migration`
→ `28 1`); local `main` was 46 behind and had never been pulled. Consequence, directly observed:
that checkout's `alembic` head was migration 0005 (`a1c3e9f4d2b7`) while the shared dev database
on port 5433 was already stamped at migration 0007 (`adf7ab9c5af2`, `origin/main`'s head) —
`origin/main`'s own `ops/host.py`, `ops/identity.py`, `ops/selfcheck.py`, `config/env.py`, and
`deploy/scripts/release.sh` did not exist there, and running that checkout's test suite against
the shared DB produced **21 failed, 210 passed, 12 skipped, 7 xfailed, 4 errors** — every failure
traced to `column releases.image_ref does not exist` (code at 0005, DB at 0007). This snapshot's
branch (`status-and-opencode`) is `origin/main` exactly (`git rev-list --left-right --count
origin/main...HEAD` → `0 0`); `opencode-migration` itself is untouched at its original commit.

### 4.2 `.claude/` on `main` is smaller than a session's own `CLAUDE.md` may assume

`origin/main`'s `.claude/` has 6 agent files, 2 hooks (`guard-applied-migrations.sh`,
`python-checks.sh`), and `settings.json` — no `.claude/skills/`, no
`.claude/scripts/merge-class-a.sh`, no `guard-protected-branch.sh`. Those five extra pieces exist
only on PR #15's branch (`rebuild-autonomous-engineering-workflow`). A Claude Code session working
from a checkout of that branch, or from stale local state, will cite files that are not on `main`.

### 4.3 PR #15 is parked — do not touch it

`gh pr view 15`: `state: OPEN`, `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY`, based on the
abandoned `milestone-7-production-deployment` rather than `main`, last touched 2026-09-13.
`CLAUDE.md`'s own Working Agreements section (verified on this branch) now says explicitly: *"PR
#15 ... is Jordan working solo — do not pick it up, review it, or merge it unless explicitly
asked."* This snapshot did not touch it.

### 4.4 `milestone-7-ci-docker-removal` exists only locally, unpushed

One commit ahead of `origin/main` (`835c63a`, "Remove Docker/Compose CI jobs and orphaned Docker
artifacts (ADR-019)"), −985/+150 lines, deleting `Dockerfile` and both compose files and
redesigning `container-build`/`publish-image`/`migration-preflight`/`staging-smoke`. `git
ls-remote --heads origin milestone-7-ci-docker-removal` returns nothing — never pushed. It lives
in a **locked** git worktree (`git worktree list` → `.claude/worktrees/adr016-fixes`, resolving to
`/home/agent/claude-finance-app/.claude/worktrees/adr016-fixes`). This exact worktree is the
incident source ADR-018 names elsewhere as prior evidence of uncommitted state silently reverting
an already-merged fix — audit its contents against `origin/main` line by line before pushing it.

### 4.5 A prior local `.venv` had a broken absolute shebang

Discovered while gathering this snapshot's test evidence: `.venv/bin/pytest` (and every other
console script) had `#!/home/agent/claude-finance-app/.venv/bin/python` baked in — a path that
does not exist (`pyvenv.cfg`'s `home = /usr/bin`, but the venv itself had been built for a
different working directory and never rebuilt here). `uv run pytest` failed with `Failed to spawn:
pytest — No such file or directory`. Fixed for this snapshot by `rm -rf .venv && uv sync`
(disposable, gitignored, not app state) — record this as a recognizable failure mode, not a
one-off.

### 4.6 Two strict `xfail`s remain, both in `tests/integration/test_deploy_gate_regression.py`

- `test_deploy_reports_failure_when_the_promotion_lock_is_contended` — **QA-21**: "when the
  promotion advisory lock is contended, `mark_healthy` records the release as `failed` but
  `finops deploy` still prints success and exits 0."
- `test_migration_0005_applies_to_a_database_that_already_hit_the_bug` — **QA-23**: "migration
  0005's unique index cannot be created on a database that already contains the duplicate rows the
  bug it fixes produced." This one is a live data-migration hazard, not just a test gap — it only
  bites a database that already ran the pre-fix `mark_healthy`.

QA-22/24/25/26 (the other four from the same review round) are resolved on `main` — confirmed
absent from this file's current xfail list.

### 4.7 The shared 5433 cluster carries state across unrelated runs

Explicitly checked and **PG16 vs PG17 is not an issue** — the cluster is `PostgreSQL 17.11
(Ubuntu, PGDG)` (`psql -c 'select version()'`), `pg_lsclusters` shows `17 main 5433 online`, local
`psql`/`pg_dump`/`pg_restore` are all 17.11, and every compose file plus every CI service pins
`postgres:17-alpine`; ADR-019 line 11 specifies PostgreSQL 17 by design. Stop re-investigating
this.

The real hazard is different: **the cluster is shared across every worktree and every past run on
this host**, and state from one bleeds into another. Two instances observed directly while writing
this snapshot: (a) §4.1's stale checkout left the shared `finance_dev` at a migration head that
checkout's own code couldn't satisfy; (b) `\l` on this cluster shows a leftover
`finance_ci_preflight` database (`select datname from pg_database where datname like
'finance%'` → `finance_dev`, `finance_ci_preflight` — matching CI's `migration-preflight` job's
`ALEMBIC_DATABASE_URL`, but with no such job configured to run against this host) still owning 52
objects tied to the cluster-wide `finance_owner` role. That pre-existing leftover is what broke
this snapshot's own test run: `tests/integration/test_migration_reversibility.py`'s three tests
downgrade `finance_dev` to `b7f6fdafce87`, which tries `DROP ROLE IF EXISTS finance_owner` — a
cluster-wide operation — and fails with `DependentObjectsStillExist` because of the unrelated
`finance_ci_preflight` database, not because of anything wrong in the migration itself. This
snapshot did not drop `finance_ci_preflight`; it is not this session's database to remove
unilaterally. Anyone re-running the full suite on this host should expect the same three failures
until that database is cleaned up or the cluster is given a fresh volume.

## 5. Doc drift found while writing this (listed, not fixed)

- `CLAUDE_FINANCE_APP_HANDOFF.md` §28's ADR list stops at ADR-014 — omits 015, 016, 017, 019 (and
  018 on PR #15's branch). Stale on every branch checked.
- `docs/plaid-sync.md`'s "Not yet built" list still names `finance-sync.timer`, which now exists
  (`deploy/systemd/finance-sync.timer`).
- `docs/analytics.md`'s "Not yet built" list still names the `finance` CLI commands and agent
  tools, both of which shipped in M4/M5.
- `docs/adr/ADR-016-production-deployment-control-plane.md:79-91` still says there is no
  long-running `app` service; `deploy/compose.yaml`'s `app` service defines
  `command: ["finance", "status"]` with `restart: unless-stopped`.

## 6. Next 4 development tasks, in order

PR #15 is intentionally excluded — `CLAUDE.md` says not to touch it unless explicitly asked.

**Docker removal is done.** PR #23 squash-merged to `main` (`79773a3`): Docker/Compose artifacts
deleted, CI's Docker-shaped jobs redesigned (`needs:` gates, `pipefail` on the archive step), and
`guards.js` fails closed for `main` when its hook file is missing. QA-17 (upgrade-in-place) is
deferred in `docs/ADR-019` and must be resolved before the first prod deploy. *Class B.*

**1. Write the bare-metal systemd units; wire `Settings.production_units`.** ADR-019's last
"Not shipped" row. Rewrite `deploy/systemd/*.service` to invoke
`/opt/finance/current/.venv/bin/{finance,finops}` instead of `docker compose run`, keeping the
existing hardening block; then `ops/host.py:run_systemctl` stops skipping and `finops deploy` can
actually restart the app. Revisit QA-25 (`PrivateTmp` vs. the restore-drill bind mount) — its
Docker-era framing no longer applies. *Class B: deploy path and credential delivery.*
Files to read first: `deploy/systemd/` (all units), `src/finance_app/ops/host.py`,
`src/finance_app/config/settings.py`, `deploy/scripts/with-production-env.sh`,
`deploy/scripts/release.sh`, `docs/runbooks/deploy.md`, `docs/adr/ADR-015-backup-encryption-gpg-symmetric.md`.

**2. Fix QA-21 and QA-23** (§4.6) — the two remaining strict xfails, both real defects with a
concrete failure scenario already written as a test. Fixing QA-23 (the migration 0005 unique index)
is itself a Class B migration change and should follow `.claude/skills/safe-migration` once that
tooling is available, or its equivalent checklist by hand otherwise. *Class B.*
Files to read first: `tests/integration/test_deploy_gate_regression.py` (both xfail tests in full),
`src/finance_app/ops/release.py` (`mark_healthy`, `_try_acquire_promotion_lock`),
`migrations/versions/0005_a1c3e9f4d2b7_release_rollback_safety.py`.

**3. Owner-performed production provisioning** (ADR-019 row 2 — explicitly out of scope for an
engineering session, and the hard gate on Milestone 7 finishing). Create `finance_prod` on the
5433 cluster, the `finance-prod` Unix user, `/opt/finance/{releases,current,.env}` (mode 600,
`FINANCE_ENV=production`), mint the systemd encrypted credentials, then run
`deploy/scripts/release.sh` and an owner-run `finops deploy` for the first release. *Owner-only —
no agent session touches `/opt/finance`, per `CLAUDE.md`'s NEVER list.*
Files to read first: `docs/runbooks/deploy.md`, ADR-019 §"Two environment files, two directories",
`docs/adr/ADR-010-production-secrets-excluded-from-engineering.md`,
`src/finance_app/config/env.py`, `deploy/scripts/release.sh`, `docs/runbooks/restore-backup.md`.

**4. Milestone 8 — Plaid webhook.** First milestone with zero work started. Minimal authenticated
endpoint, `SYNC_UPDATES_AVAILABLE` handling, concurrency via the Postgres advisory lock
`plaid/sync.py` already implements (`_single_sync_lock`) — §6.4 explicitly forbids adding
Redis/Celery for this — host-installed Caddy, daily timer retained as the reconciliation fallback
(ADR-012). Needs an owner decision on domain/DNS (handoff §32 names this as a legitimate
escalation). *Class B: webhook security.*
Files to read first: `CLAUDE_FINANCE_APP_HANDOFF.md` §6.4 and §29 "Milestone 8",
`docs/adr/ADR-012-daily-sync-reconciliation-fallback.md`, `src/finance_app/plaid/sync.py`
(`_single_sync_lock`, `run_daily_sync`), `deploy/caddy/Caddyfile`, `.env.example`
(`PLAID_WEBHOOK_SECRET`), `docs/plaid-sync.md`, `docs/runbooks/README.md`.
