# Security Model

Derived from `CLAUDE_FINANCE_APP_HANDOFF.md` §4. These invariants override convenience. If a task appears to require violating one, the task is designed wrong.

## What we are protecting

One person's complete checking-account transaction history, plus the credentials that grant ongoing access to it. There is no multi-tenant blast radius here — the entire risk is concentrated in a single dataset and a single Plaid access token.

## Trust boundaries

```
┌─ Engineering boundary ────────────────────────────────────────┐
│  Claude Code + subagents, CI runners, dev containers          │
│  Has: Plaid Sandbox creds, synthetic data, repo write access  │
│  Never has: production Plaid token, production DB owner creds,│
│             production runtime-agent API key (OpenAI or       │
│             Anthropic), shell on the finance VPS               │
└───────────────────────────────────────────────────────────────┘
                              │  immutable image, by SHA
                              v
┌─ Production runtime boundary ─────────────────────────────────┐
│  The VPS. systemd encrypted credentials. Real financial data. │
│  Reachable by the owner over SSH, and by Plaid at the webhook │
│  endpoint if enabled. Nothing else.                           │
└───────────────────────────────────────────────────────────────┘
                              │  semantic tools only
                              v
┌─ Runtime agent boundary ──────────────────────────────────────┐
│  Provider-interchangeable conversational agent — OpenAI or    │
│  the Claude API, selected by AGENT_PROVIDER (ADR-014).        │
│  Has: a fixed set of validated, parameterized tools.          │
│  Never has: SQL, shell, filesystem, network, Plaid creds,     │
│             or any write path to plaid.*                      │
└───────────────────────────────────────────────────────────────┘
```

Autonomy is granted through source control, test environments, and narrow interfaces — never through production credentials.

## Invariants

### 1. Raw Plaid data is immutable to interpretation

`plaid.*` is written only by deterministic ingestion and reconciliation code. Categorization, notes, and tags are written to separate tables and composed at read time. Provenance is never destroyed. Enforced by database grants, not by convention, and proven by a test that asserts `finance_agent` cannot `UPDATE`/`DELETE`/`INSERT` on `plaid.*`.

### 2. No arbitrary SQL for the agent

There is no `run_sql(query: str)`, and adding one is out of scope permanently. The agent selects a business operation from a fixed semantic vocabulary; deterministic Python and parameterized SQL execute it. Every write tool validates inputs, is audited to `agent.tool_calls`, and returns a structured description of what it changed.

### 3. Financial arithmetic is deterministic

Sums, averages, medians, percentage changes, budget variance, cashflow, savings rate — all computed in SQL or Python. The model explains; it does not calculate. Money is `NUMERIC`/`DECIMAL` or integer minor units, never binary floating point.

### 4. Production credentials are isolated

Protected material: Plaid client ID, Plaid production secret, Plaid access token, runtime agent provider API key(s) (OpenAI and/or Anthropic, per ADR-014), PostgreSQL credentials, webhook secret, backup encryption key.

Stored as systemd encrypted credentials on the VPS. Never in committed files, never in a plaintext `.env`, never in CI, never in an agent session. `.claude/settings.json` denies reads of credential paths and blocks `ssh`/`scp`/`rsync`, so the engineering environment has no route to production even if a session misjudges.

**Known gap: secrets still cross into container environment variables.** `with-production-env.sh` decrypts each systemd credential and exports it as a plain process environment variable so `docker compose`'s `${VAR}` interpolation can reach it; `deploy/compose.yaml` then passes those variables into each service's `environment:` block. That is stronger than a plaintext file on disk (credentials never touch the filesystem unencrypted, and `docker compose` itself never persists them), but it is weaker than the invariant's framing implies once a container is actually running: for that container's lifetime, its secrets are ordinary process environment variables, visible to anything with `docker inspect` access, a shell in the container, or `/proc/<pid>/environ` on the host. Docker-native file-based secrets (bind-mounting `$CREDENTIALS_DIRECTORY` into each container and reading `*_FILE` paths instead of `*` env vars) would close this, but that is a topology change across every service in `deploy/compose.yaml` and the settings-loading path in `finance_app.config`, not a fix that belongs in an unrelated bug-fix pass. Tracked as a follow-up; see `docs/backups.md`'s "Deliberately deferred" section for the same note in the backup-specific context.

### 5. Least privilege in the database

| Role | Purpose |
|---|---|
| `finance_owner` | Rarely used; not a normal application path |
| `finance_migrator` | Schema changes during controlled release only |
| `finance_app` | Deterministic application writes, including Plaid ingestion |
| `finance_agent` | Read curated views; write only `user.*`, `finance.*`, `agent.*` |
| `finance_observer` | Sanitized read-only operational data for maintenance |
| `finance_backup` | Backup/restore only |

The runtime tool layer never inherits owner or migrator privileges.

### 6. Logs are sanitized

Never logged by default: access tokens, authorization headers, database passwords, API keys, account or routing numbers, full financial payloads, or prompts containing unnecessary transaction detail. Log structured operational events — counts, run IDs, durations, status codes, sanitized exception classes.

### 7. Migrations are controlled

Applied migrations are history and are never rewritten; corrections go forward. No automatic table drops. No destructive schema change without explicit migration safety tests. A `PreToolUse` hook blocks edits to committed migration files.

### 8. No single model authors, reviews, and ships a consequential change

Class B work requires implementation, then independent specialist review, then adversarial tests, then security review, then staging — as separate agent invocations with separate context. The `security-reviewer` subagent deliberately has no write tools.

## Threat model

| Threat | Mitigation |
|---|---|
| Plaid access token exfiltration | Encrypted credentials; never in repo, CI, logs, or agent context; deny rules on credential paths |
| Prompt injection via merchant name or transaction note | Attacker-influenced strings are data, never instructions; tool authority is fixed and cannot be widened by a tool result; QA agent actively tests this |
| Agent tricked into destroying financial data | No SQL/shell tool exists; `finance_agent` grants make `plaid.*` writes impossible at the database level |
| Model fabricates a financial figure | All numbers come from deterministic queries; evals assert exact values against a golden dataset |
| Compromised dependency reaching production | Pinned dependencies and actions, secret scanning, dependency scanning in CI, no self-hosted runner on the VPS |
| Malicious or replayed webhook | Signature verification before any side-effecting parse; replay resistance; the endpoint does nothing but request a sync |
| Silent data loss during sync | Cursor advances only behind persisted writes; unique constraints make replay idempotent; daily reconciliation as safety net |
| Ransomware / host loss | Encrypted off-machine backups with scheduled, verified restore tests |
| Engineering agent misjudgment | Compartmentalized authority — no production credentials, no SSH, no direct VPS editing, Git-only release path |

## Incident classes

**Class A** — lint, formatting, clear test regressions, logging, patch dependencies. Autonomous repair and merge through normal gates (CI required-green). Merge is not deploy — production deploy is always an owner-performed `finops deploy` regardless of class; see `docs/deployment.md`'s release sequence and ADR-017.

**Class B** — migrations, sync semantics, financial math, credential handling, webhook security, agent permissions. Autonomous, but with full independent review gates before production.

**Class C** — suspected credential compromise, unexplained financial data corruption, lost source-of-truth records, repeated failed restores, backup failure alongside integrity concerns, suspected unauthorized access, or a reconciliation discrepancy that cannot be explained from Plaid source changes.

For Class C: stop destructive and automatic repair, preserve logs and evidence, take safe snapshots, write a concise incident report with recommended actions, and require owner authorization for anything that could destroy evidence or financial records.

**Refusing an unsafe mutation is correct autonomous behavior, not a failure to act.**

## Verification

Security invariants are tested, not asserted. `tests/security/` must cover at minimum: the `finance_agent` role cannot mutate `plaid.*`; no arbitrary SQL tool exists in the tool registry; secrets do not appear in log output; production secret paths are not mounted into dev or CI; webhook input validation; and dependency and secret scanning in CI.
