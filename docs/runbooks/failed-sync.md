# Runbook: diagnosing a failed or stalled Plaid sync

Diagnosis only uses `finops` — never `psql`/ad hoc shell against the sync tables (ADR-007, handoff §10). See `docs/plaid-sync.md` for the sync design this runbook diagnoses.

## 1. Check the signal

```
finops sync-status
```

Fields to read:

- `status` — `success`, `error`, `running` (a sync is currently in progress or was interrupted mid-run without updating status — see step 4), `stale` (last successful sync is more than ~36h old — the daily timer itself may not be firing), or `never_run`.
- `last_run.last_error` — a sanitized error message/category from the most recent attempt.
- `cursor_present` — whether `plaid.sync_state` has a stored cursor. `false` after a healthy history usually means the cursor was reset (e.g. by a Milestone-2-style Item re-link) rather than data loss — the next sync will do a full initial sync, not silently skip anything.
- `item_status` — the Plaid Item's own status; `ITEM_LOGIN_REQUIRED` here means re-authentication is needed (see step 3).

Also check `finops recent-errors --limit 20` for anything in `ops.errors` with `category` starting `sync` or `plaid`.

## 2. Common causes and what they look like

| Symptom | Likely cause | Action |
|---|---|---|
| `status: error`, error mentions rate limit / timeout | Transient Plaid API issue | Usually self-heals on the next scheduled run; re-run manually if urgent (`sudo systemctl start finance-sync.service`) |
| `status: error`, `ITEM_LOGIN_REQUIRED` | The Item needs re-authentication (bank changed credentials, MFA expired, etc.) | Owner runs Link's "update mode" — `docs/runbooks/plaid-link.md`'s rotation section |
| `status: stale`, no recent `last_run` at all | `finance-sync.timer` itself isn't firing | On the VPS: `systemctl list-timers finance-sync.timer` and `journalctl -u finance-sync.service` — a systemd-level problem, not a sync-logic problem |
| `status: error`, database-shaped error (connection refused, etc.) | Postgres is down or unreachable | `finops db-status` first — this is a database incident, not a sync incident |
| `cursor_present: false` after previously being `true`, with no re-link | Unexpected — do **not** assume "just re-sync will fix it" | Treat as Class B/C per below — this could indicate `ops.sync_runs`/`plaid.sync_state` data loss, not a normal sync failure |

## 3. Re-authentication (`ITEM_LOGIN_REQUIRED`)

This is expected, ordinary Plaid behavior (banks periodically require re-auth) — not a Class B/C incident by itself. Follow `docs/runbooks/plaid-link.md`'s "Rotation / re-authentication" section: the owner performs Link's update-mode flow and replaces the stored `plaid_access_token` credential (`docs/runbooks/deploy.md` §7). The underlying `plaid.items` row and all historical transactions are unaffected.

## 4. A sync stuck at `status: running`

If `finops sync-status` reports `running` for far longer than a sync should take (see `docs/plaid-sync.md` for expected duration), the process likely crashed or was killed mid-run without reaching its failure handler. Check:

```
journalctl -u finance-sync.service --since "-2h"
```

on the VPS. If the process is confirmed dead (not actually still running), the next scheduled sync attempt will start a new run — the cursor-based design (`docs/plaid-sync.md` §6.2) means an interrupted run cannot lose data, only delay it, because the cursor only advances behind durably persisted writes. This is a Class A situation (retry/recovery is exactly what the design anticipates) unless it recurs repeatedly, in which case escalate to Class B (something about the sync loop itself may be broken) or Class C if it correlates with unexplained data changes.

## 5. When to escalate past this runbook

- The same failure recurs across multiple scheduled runs with no external explanation (not a rate limit, not a known Plaid outage, not a re-auth need) — Class B: this needs code-level investigation, not another retry.
- `recent-errors`/sync counts suggest transactions were lost or duplicated in a way `added`/`modified`/`removed` reconciliation shouldn't produce — Class C: stop, follow `docs/runbooks/incident-class-c.md`, do not attempt a "just re-sync it" fix first.
