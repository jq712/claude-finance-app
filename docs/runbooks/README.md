# Runbooks

Owner-facing operational procedures. Each runbook is written so the owner can follow it under stress, and so an agent can follow it without improvising.

Planned (created with the milestone that makes them real):

| Runbook | Milestone |
|---|---|
| `plaid-link.md` — initial Link, token exchange, credential storage, update mode, rotation | 2 |
| `deploy.md` — normal release and rollback | 7 |
| `restore-backup.md` — restore into a clean instance and verify | 7 |
| `failed-sync.md` — diagnosing a failed or stalled sync via `finops` | 7 |
| `incident-class-c.md` — evidence preservation and escalation | 9 |

When an incident reveals a procedure that was not written down, write it here in the same change that resolves the incident.
