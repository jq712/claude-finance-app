# Incident Response

Milestone 7 deliverable (handoff §25, §29). `docs/security-model.md` defines the Class A/B/C framework this document makes operational: how to actually classify what's happening, which `finops` commands to run, and what "stop and escalate" looks like in practice. See `docs/runbooks/failed-sync.md` and `docs/runbooks/incident-class-c.md` for the two most common concrete procedures this document generalizes.

## First move, always: `finops health`

```
finops health
```

Read-only (`finance_observer`), exits non-zero when unhealthy, and its component breakdown (`database`, `migrations`, `sync`, `backup`) is the fastest way to narrow down which of the four things below is actually wrong before doing anything else. Follow up with the specific command for whichever component is unhealthy:

```
finops db-status          # database reachable?
finops migration-status   # applied revision matches repo head?
finops sync-status        # most recent Plaid sync run and cursor state
finops backup-status      # most recent backup, and is it restore-verified?
finops recent-errors      # sanitized ops.errors — never a financial payload
```

None of these require SQL, shell, or SSH access to run — they're the entire point of `finops` existing (handoff §10, ADR-007). If a signal you need isn't exposed by one of these commands, the fix is to add the command, not to reach around the interface with `psql`.

## Classify before acting (handoff §25)

| | Examples | Response |
|---|---|---|
| **Class A** | Lint/formatting, a clear test regression, a logging bug, a patch dependency bump, a transient sync retry that already recovered | Diagnose and fix through the normal pipeline (`docs/deployment.md`'s release sequence). No special gate beyond ordinary CI. |
| **Class B** | A migration, Plaid sync semantics, financial calculation logic, credential handling, webhook security, agent permission changes | Implementation, then independent specialist review, then adversarial tests, then security review, then staging — as separate agent invocations, per CLAUDE.md's delegation rule. Never the same session that wrote the change approving it. |
| **Class C** | Suspected credential compromise, unexplained financial data corruption, lost source-of-truth `plaid.*` records, repeated failed restores, a backup failure alongside integrity concerns, suspected unauthorized access, a reconciliation discrepancy `finops sync-status`/`recent-errors` can't explain from an ordinary Plaid source change | **Stop.** See below. |

Getting the classification wrong in the cautious direction (treating a Class A issue with Class B/C care) costs time. Getting it wrong in the other direction — treating data corruption as an ordinary bug — can destroy evidence or compound the damage. When genuinely unsure, classify up.

## Class C: stop, preserve, escalate

This is the one case where **the correct autonomous action is to refuse to act further**, not to find a fix.

1. **Stop destructive/automatic repair.** No `finops rollback`, no migration downgrade, no manual data correction, no "let me just fix this row" — any of these can destroy the evidence needed to understand what actually happened.
2. **Preserve evidence.**
   - `finops recent-errors --limit 200 --json` — capture sanitized operational error history.
   - `journalctl -u finance-sync -u finance-app -u finance-backup -u finance-health --since "-48h" > incident-logs.txt` on the VPS (owner-performed — this is exactly the kind of production shell access the engineering environment never has).
   - Note the current release: `finops version`.
3. **Take a safe snapshot before anything else touches the database.** An out-of-band `finops` backup is read-only from the database's perspective (`finance_backup` role, `SELECT` only) — running one now does not risk further mutation, and it captures the corrupted/compromised state for later analysis, which is valuable evidence even though it isn't a "clean" backup to restore from.
4. **Write a concise incident report.** What was observed, when, which `finops` outputs support the classification, what's suspected, what has and hasn't been touched. This becomes the durable record — commit it or attach it to an issue, per CLAUDE.md's "documentation as long-term agent memory."
5. **Require owner authorization for anything that could destroy evidence or financial records** — including a restore, a rollback, or a migration downgrade. The owner decides the next step from the incident report; an autonomous agent does not unilaterally proceed past this point.

If the suspected compromise involves a credential (Plaid access token, a database role password, `BACKUP_ENCRYPTION_KEY`, a runtime provider API key), rotation is always an **owner-performed** runbook step (`docs/security-model.md` invariant 4; `docs/runbooks/deploy.md`'s rotation procedure) — the engineering environment never holds the material needed to rotate a production credential itself.

## Class B: full gates, still autonomous

A Class B fix still moves through the entire pipeline in `docs/deployment.md` — CI, migration-preflight, staging-smoke, critical agent evals, the `production` environment's manual-approval gate, then an owner-performed `finops deploy`. The difference from Class A is *who reviews it*: per CLAUDE.md's delegation rule, the subagent that implements a Class B change is never the one that approves it — `security-reviewer` and `qa-adversarial` run as separate invocations with their own context, and review-oriented subagents carry no write tools by design (`.claude/agents/`).

## Rollback as an incident response tool

If a newly deployed release is the cause (a bad migration, a regression that only shows up against real data shape), `finops rollback` is the fastest safe response — it requires no rebuild and no registry fetch beyond what's already local (ADR-008), and it's exactly what `finops deploy`'s own post-deploy health check triggers automatically on failure. Rollback is *not* an appropriate Class C response, though — reverting the application does nothing to preserve or explain evidence of data corruption or credential compromise, and per the Class C procedure above, no destructive action (rollback included) happens before evidence is preserved and the owner has been looped in.

## After the incident

Per CLAUDE.md §"Durable learning": encode what was learned as one or more of a regression test, an eval, a `CLAUDE.md`/runbook update, a new ADR, or — if the rule must hold even when a future session forgets it — a hook. An incident that doesn't leave a durable trace in the repository will recur.
