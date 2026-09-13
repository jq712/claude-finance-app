# Runbook: restoring a backup

**Rewritten 2026-09-13 for ADR-019** (bare-metal, no Docker) — supersedes this runbook's previous `docker compose`/`with-production-env.sh`-per-job procedure. Two audiences: the automated weekly drill (`finance-restore-drill.timer`, no owner action needed — see `docs/backups.md`), and an owner-performed disaster-recovery restore, which is what this runbook covers. See `docs/backups.md` for the pipeline design and `docs/adr/ADR-015-backup-encryption-gpg-symmetric.md` for the encryption approach.

## When to use this

- The production database was lost or corrupted (disk failure, accidental `DROP`, etc.) and needs to be rebuilt from the most recent backup.
- You want to inspect a historical backup's contents without touching production (e.g. to answer "what did this budget look like three weeks ago").

If you suspect the corruption itself is suspicious (not an ordinary hardware/operator failure) — see `docs/runbooks/incident-class-c.md` first. Restoring over evidence of a Class C incident can destroy exactly what needs to be preserved.

## Setup

Every command below runs as the `finance-prod` Unix user, with `/opt/finance/.env` sourced into the environment (e.g. `sudo -u finance-prod bash -c 'set -a; source /opt/finance/.env; set +a; exec <cmd>'`, or however the follow-up implementation wraps this) — never from the engineering session, which cannot read that file (`docs/security-model.md`'s "Trust boundaries"). Read any password prompted for interactively (`read -rs`), never on the command line — a positional argument lands in shell history the moment it's typed.

## Restoring into a scratch database (safe default — inspection, drills, most disaster-recovery rehearsals)

```
cd /opt/finance/current
sudo -u finance-prod ./deploy/scripts/restore.sh <backup-path> <target-database-url>
```

- `<backup-path>` — an absolute path under `BACKUP_DIR` (e.g. `/opt/finance/backups/...`). List available backups: `sudo -u finance-prod .venv/bin/python -m finance_app.ops.backup latest`.
- `<target-database-url>` — a SQLAlchemy-style URL for a **scratch** database, e.g. a throwaway database on the same host PostgreSQL instance created just for this restore (exactly what `deploy/scripts/restore-verify.sh` automates for the weekly drill — read that script for the pattern once it's rewritten for bare-metal, or adapt it by hand).

`pg_restore --clean --if-exists` drops existing objects in the target before restoring, so double-check `<target-database-url>` points at the scratch database, not `finance_prod`, before running this.

After restoring, run the same sanity checks the weekly drill runs:

```
sudo -u finance-prod .venv/bin/python -m finance_app.ops.backup verify <backup-run-id> --target-url <target-database-url>
```

`<backup-run-id>` is printed by the `latest` command above, or found via `finops backup-status`.

## Full disaster recovery (restoring over production — rare, deliberate, high-stakes)

This is not a `finops` one-liner on purpose — it is the one operation in this system where "narrow, semantic command" would hide how consequential the action is. Treat it as a Class B change even though no code changes: implementation (the steps below), then a second pair of eyes if at all possible before the final step.

1. Stop the application so nothing writes to the database mid-restore:
   ```
   sudo systemctl stop finance-app.service
   ```
2. Confirm which backup you're restoring and why (`finops backup-status`, and the incident record if this follows an incident).
3. Restore directly into `finance_prod` on the host PostgreSQL instance — the one case where the target is *not* a scratch database. Set `$TARGET_DATABASE_URL` rather than passing it positionally (lands in shell history otherwise), and read the password interactively, in the same shell invocation that uses it — never `export`ed somewhere it could leak into a process listing or unit property:
   ```
   sudo -u finance-prod sh -c '
       set -a; . /opt/finance/.env; set +a
       read -rs -p "finance_migrator password: " FINANCE_MIGRATOR_DB_PASSWORD; echo
       export TARGET_DATABASE_URL="postgresql+psycopg://finance_migrator:${FINANCE_MIGRATOR_DB_PASSWORD}@localhost:5432/finance_prod"
       unset FINANCE_MIGRATOR_DB_PASSWORD
       exec /opt/finance/current/deploy/scripts/restore.sh "$1" "" "$2"
   ' _ <backup-path> <backup-run-id>
   ```
   (`<backup-run-id>` — from `python -m finance_app.ops.backup latest` or `finops backup-status` — is optional but strongly recommended here: it verifies the artifact's recorded sha256 before decrypting, the one channel that would otherwise let a substituted or corrupted-in-place backup silently restore over production.)
4. Run the sanity checks (previous section) against the now-restored `finance_prod` database.
5. Confirm `finops migration-status` reports `up_to_date` — a backup taken before a since-applied migration will need `alembic upgrade head` run afterward.
6. Restart the application:
   ```
   sudo systemctl start finance-app.service
   ```
7. Confirm `finops health` is clean, then run a manual `finance sync` (`finance-sync.service`) to catch up on anything since the backup — the daily sync's `added`/`modified`/`removed` reconciliation is designed for exactly this "the local state is stale" situation.
8. Write down what happened and why, per `docs/incident-response.md`'s "after the incident" section, even if this wasn't triggered by a Class C incident.

## What this does not cover

Restoring to a *different* PostgreSQL major version than the backup was taken from, or restoring across an off-machine storage transfer — both depend on decisions (see "Deliberately deferred" in `docs/backups.md`) that haven't been made yet, since no off-machine backup destination exists.
