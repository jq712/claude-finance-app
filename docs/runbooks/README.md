# Runbooks

Owner-facing operational procedures. Each runbook is written so the owner can follow it under stress, and so an agent can follow it without improvising.

Written:

| Runbook | Milestone |
|---|---|
| [`plaid-link.md`](plaid-link.md) — initial Link, token exchange, credential storage, update mode, rotation | 2 |
| [`deploy.md`](deploy.md) — VPS provisioning, minting credentials, first deploy, normal release, rollback, restart, credential rotation | 7 |
| [`restore-backup.md`](restore-backup.md) — restore into a scratch instance and verify, plus full disaster recovery | 7 |
| [`failed-sync.md`](failed-sync.md) — diagnosing a failed or stalled sync via `finops` | 7 |
| [`incident-class-c.md`](incident-class-c.md) — evidence preservation and escalation | 7 (brought forward from 9 — Milestone 7's backup/incident work made this concrete sooner than planned) |
| [`autonomous-continuation.md`](autonomous-continuation.md) — what an unattended autonomous-continuation session does, and what to review after a run | 7 (brought forward from 9 — see ADR-017) |

Planned (created with the milestone that makes them real):

| Runbook | Milestone |
|---|---|
| `webhook.md` — provisioning the webhook domain/TLS, enabling the `caddy` Compose profile, webhook secret rotation | 8 |

When an incident reveals a procedure that was not written down, write it here in the same change that resolves the incident.
