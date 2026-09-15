# Security Model

Derived from `CLAUDE_FINANCE_APP_HANDOFF.md` §4. These invariants override convenience. If a task appears to require violating one, the task is designed wrong.

## What we are protecting

One person's complete checking-account transaction history, plus the credentials that grant ongoing access to it. There is no multi-tenant blast radius here — the entire risk is concentrated in a single dataset and a single Plaid access token.

## Trust boundaries

**As of 2026-09-13** (ADR-007/ADR-010/ADR-019): the engineering workspace (this repository) and the production deployment share one physical VPS — the owner's deliberate choice. There is no Docker anywhere in this application (ADR-019 supersedes the Compose-based design ADR-016 described). The boundary below is a **directory, Unix-user, and tool-policy boundary on a single host**, not a host/network boundary — say so plainly rather than describing a wall that doesn't exist.

```
┌─ Engineering boundary ────────────────────────────────────────┐
│  Claude Code + subagents, CI runners                          │
│  Runs as an unprivileged user in this repository's directory, │
│  on the SAME VPS the production deployment runs on.           │
│  Has: Plaid Sandbox creds, synthetic data in `finance_dev`,   │
│       repo write access, an unprivileged local shell on the   │
│       production host                                          │
│  Never has: production Plaid token, production DB owner creds,│
│             production runtime-agent API key (OpenAI or       │
│             Anthropic), root/sudo, read access to /opt/finance │
│             or its .env                                        │
└───────────────────────────────────────────────────────────────┘
     │ same host, different directory + different Unix user;
     │ deployed code is a copied, CI-green git ref (ADR-019) —
     │ never this working tree's uncommitted or unpushed state
     v
┌─ Production runtime boundary ─────────────────────────────────┐
│  Same VPS as engineering. /opt/finance (release directories + │
│  a `current` symlink), owned by a separate, more-privileged   │
│  Unix user the engineering session's user is not a member of. │
│  systemd units invoke /opt/finance/current's own virtualenv   │
│  directly. Credentials: /opt/finance/.env (mode 600) plus any │
│  systemd-creds-backed value, decrypted only inside the unit    │
│  that needs it. The `finance_prod` database, on the same host │
│  PostgreSQL instance as `finance_dev` but a separate logical   │
│  database. Real financial data. Reachable by the owner over   │
│  SSH/console, and by Plaid at the webhook endpoint if enabled.│
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

Autonomy is granted through source control, test environments, and narrow interfaces — never through production credentials. **What this boundary can no longer prevent:** the engineering session sharing a host with production means a mistake here (disk exhaustion, an errant destructive command, CPU/memory contention) can degrade production *availability* even without ever touching a credential — categorically impossible under host separation, now accepted, not mitigated. **What it still protects, and how:** production *confidentiality* is gated behind (a) `/opt/finance` and its `.env` being owned by a Unix user the engineering session's user cannot read as, (b) that session having no `sudo` (verified: no passwordless sudo is configured) and, since there is no Docker, no other route to root-equivalent host access either, and (c) `.claude/settings.json`'s policy-level denials (credential-path reads, `systemd-creds`, `ssh`/`scp`/`rsync`) still in force. (a) and (b) are OS-enforced permission checks; (c) is Claude Code's own configured policy, defense-in-depth on top of (a)/(b), not a replacement for them.

## Invariants

### 1. Raw Plaid data is immutable to interpretation

`plaid.*` is written only by deterministic ingestion and reconciliation code. Categorization, notes, and tags are written to separate tables and composed at read time. Provenance is never destroyed. Enforced by database grants, not by convention, and proven by a test that asserts `finance_agent` cannot `UPDATE`/`DELETE`/`INSERT` on `plaid.*`.

### 2. No arbitrary SQL for the agent

There is no `run_sql(query: str)`, and adding one is out of scope permanently. The agent selects a business operation from a fixed semantic vocabulary; deterministic Python and parameterized SQL execute it. Every write tool validates inputs, is audited to `agent.tool_calls`, and returns a structured description of what it changed.

### 3. Financial arithmetic is deterministic

Sums, averages, medians, percentage changes, budget variance, cashflow, savings rate — all computed in SQL or Python. The model explains; it does not calculate. Money is `NUMERIC`/`DECIMAL` or integer minor units, never binary floating point.

### 4. Production credentials are isolated

Protected material: Plaid client ID, Plaid production secret, Plaid access token, runtime agent provider API key(s) (OpenAI and/or Anthropic, per ADR-014), PostgreSQL credentials, webhook secret, backup encryption key.

Stored in `/opt/finance/.env` (mode 600) and as systemd encrypted credentials for anything a unit needs decrypted at start. Never in committed files, never in this repository's `.env`/`.env.dev`, never in CI, never in an engineering session. `.claude/settings.json` denies reads of credential paths and blocks `ssh`/`scp`/`rsync`/`systemd-creds`, but on a shared VPS (ADR-007/ADR-010/ADR-019, revised 2026-09-13) that is policy enforcement, not network isolation — the actual technical backstop is that `/opt/finance` is owned by a Unix user the engineering session's user cannot read as, and that session has no `sudo`.

**Resolved by ADR-019, was an open gap under the Docker design:** the old Compose-based topology decrypted each systemd credential and exported it as a plain process environment variable so `docker compose`'s `${VAR}` interpolation could reach it, which meant that for a running container's lifetime its secrets were visible to anything with `docker inspect` access or a shell in the container — an exposure surface beyond the credential file itself. There is no Docker now, so that specific surface (`docker inspect`, container-escape-to-secret) no longer exists. The baseline risk of *any* process holding a credential in its own environment (readable via `/proc/<pid>/environ` by the same user or root) is not Docker-specific and still applies to whatever mechanism the bare-metal systemd units use — worth designing, when this is implemented, so `Settings` reads credential *files* directly from `$CREDENTIALS_DIRECTORY` (systemd's `LoadCredentialEncrypted=` decrypts to files there already) rather than requiring an env-var export step that no longer serves any purpose once nothing needs `${VAR}`-style Compose interpolation.

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
| Engineering agent misjudgment | Compartmentalized authority — no production credentials, no root/sudo, no read access to `/opt/finance`, Git-only release path (ADR-019). Shared-host availability risk (resource exhaustion, an errant destructive command) is accepted, not mitigated — see "Trust boundaries" |
| Engineering session running the dev CLI against `finance_prod` by mistake | The CLI's DSN default resolves to `finance_dev` from this repository's `.env`/`.env.dev`; reaching `finance_prod` requires an explicit prod env path, never a default (ADR-019) |

## Incident classes

**Class A** — lint, formatting, clear test regressions, logging, patch dependencies. Autonomous repair and merge through normal gates (CI required-green). Merge is not deploy — production deploy is always an owner-performed `finops deploy` regardless of class; see `docs/deployment.md`'s release sequence and ADR-017.

**Class B** — migrations, sync semantics, financial math, credential handling, webhook security, agent permissions. Autonomous, but with full independent review gates before production.

**Class C** — suspected credential compromise, unexplained financial data corruption, lost source-of-truth records, repeated failed restores, backup failure alongside integrity concerns, suspected unauthorized access, or a reconciliation discrepancy that cannot be explained from Plaid source changes.

For Class C: stop destructive and automatic repair, preserve logs and evidence, take safe snapshots, write a concise incident report with recommended actions, and require owner authorization for anything that could destroy evidence or financial records.

**Refusing an unsafe mutation is correct autonomous behavior, not a failure to act.**

## Verification

Security invariants are tested, not asserted. `tests/security/` must cover at minimum: the `finance_agent` role cannot mutate `plaid.*`; no arbitrary SQL tool exists in the tool registry; secrets do not appear in log output; production secret paths are not readable from dev or CI; the CLI's default DSN resolves to `finance_dev`, never `finance_prod`, absent an explicit prod env path; webhook input validation; and dependency and secret scanning in CI. Since the engineering/production boundary is Unix-permission-based rather than host-based (see "Trust boundaries"), add an operational check to the deploy runbook — not a CI test, CI doesn't run on the VPS — confirming periodically that the engineering session's user cannot read `/opt/finance` and has no `sudo`. Permission drift here is a silent, total loss of the confidentiality protection this document describes.
