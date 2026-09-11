# Runbook: restoring a backup

Two audiences: the automated weekly drill (`finance-restore-drill.timer`, no owner action needed — see `docs/backups.md`), and an owner-performed disaster-recovery restore, which is what this runbook covers. See `docs/backups.md` for the pipeline design and `docs/adr/ADR-015-backup-encryption-gpg-symmetric.md` for the encryption approach.

## When to use this

- The production database was lost or corrupted (disk failure, accidental `DROP`, etc.) and needs to be rebuilt from the most recent backup.
- You want to inspect a historical backup's contents without touching production (e.g. to answer "what did this budget look like three weeks ago").

If you suspect the corruption itself is suspicious (not an ordinary hardware/operator failure) — see `docs/runbooks/incident-class-c.md` first. Restoring over evidence of a Class C incident can destroy exactly what needs to be preserved.

## Setup: running `with-production-env.sh` over SSH

Like `finops deploy`/`rollback`/`restart` (`docs/runbooks/deploy.md` §2.5),
`with-production-env.sh` only decrypts credentials inside a systemd unit
declaring `LoadCredentialEncrypted=` — a bare SSH shell has no route to
that. The commands below all run under job `backup`, whose credential set
(`deploy/scripts/with-production-env.sh`'s matrix) is
`FINANCE_BACKUP_DB_PASSWORD`, `FINANCE_APP_DB_PASSWORD`,
`BACKUP_ENCRYPTION_KEY`:

```
backup_run() {
    sudo systemd-run --pty --wait --collect --same-dir \
        --property=LoadCredentialEncrypted=finance_backup_db_password:/etc/finance-app/credentials/finance_backup_db_password.cred \
        --property=LoadCredentialEncrypted=finance_app_db_password:/etc/finance-app/credentials/finance_app_db_password.cred \
        --property=LoadCredentialEncrypted=backup_encryption_key:/etc/finance-app/credentials/backup_encryption_key.cred \
        -- /opt/finance-app/deploy/scripts/with-production-env.sh backup -- "$@"
}
```

Paste that once per SSH session; every command below is `backup_run <cmd...>`.

## Restoring into a scratch database (safe default — inspection, drills, most disaster-recovery rehearsals)

```
cd /opt/finance-app
backup_run deploy/scripts/restore.sh <backup-path> <target-database-url>
```

- `<backup-path>` — an absolute path *as seen inside the app container*, i.e. under `/var/lib/finance-app/backups/...` (the mounted backup volume). List available backups: `docker compose -f deploy/compose.yaml run --rm app ls -la /var/lib/finance-app/backups`.
- `<target-database-url>` — a SQLAlchemy-style URL for a **scratch** database, e.g. a throwaway `postgres:17-alpine` container on the `finance-app` Docker network (exactly what `deploy/scripts/restore-verify.sh` automates — read that script for the pattern if you're doing this by hand).

`pg_restore --clean --if-exists` drops existing objects in the target before restoring, so double-check `<target-database-url>` is not production before running this.

After restoring, run the same sanity checks the weekly drill runs:

```
backup_run docker compose -f deploy/compose.yaml --profile app run --rm -T app \
    python -m finance_app.ops.backup verify <backup-run-id> --target-url <target-database-url>
```

`<backup-run-id>` is printed by `docker compose ... run --rm app python -m finance_app.ops.backup latest`, or found via `finops backup-status`.

## Full disaster recovery (restoring over production — rare, deliberate, high-stakes)

This is not a `finops` one-liner on purpose — it is the one operation in this system where "narrow, semantic command" would hide how consequential the action is. Treat it as a Class B change even though no code changes: implementation (the steps below), then a second pair of eyes if at all possible before the final step.

1. Stop the application so nothing writes to the database mid-restore:
   ```
   sudo systemctl stop finance-app.service
   ```
2. Confirm which backup you're restoring and why (`finops backup-status`, and the incident record if this follows an incident).
3. Restore directly into the running `postgres` service's `finance` database — the one case where the target is *not* a scratch database. Set `$TARGET_DATABASE_URL` rather than passing it positionally — a positional argument lands in shell history the moment it's typed (finding 2):
   ```
   read -rs -p "finance_migrator password: " FINANCE_MIGRATOR_DB_PASSWORD; echo
   export TARGET_DATABASE_URL="postgresql+psycopg://finance_migrator:${FINANCE_MIGRATOR_DB_PASSWORD}@postgres:5432/finance"
   unset FINANCE_MIGRATOR_DB_PASSWORD
   backup_run deploy/scripts/restore.sh <backup-path> "" <backup-run-id>
   unset TARGET_DATABASE_URL
   ```
   (`<backup-run-id>` — from `docker compose ... run --rm app python -m finance_app.ops.backup latest` or `finops backup-status` — is optional but strongly recommended here: it verifies the artifact's recorded sha256 before decrypting, the one channel that would otherwise let a substituted or corrupted-in-place backup silently restore over production.)
4. Run the sanity checks (previous section) against the now-restored `finance` database.
5. Confirm `finops migration-status` reports `up_to_date` — a backup taken before a since-applied migration will need `alembic upgrade head` run afterward.
6. Restart the application:
   ```
   sudo systemctl start finance-app.service
   ```
7. Confirm `finops health` is clean, then run a manual `finance sync` (`finance-sync.service`) to catch up on anything since the backup — the daily sync's `added`/`modified`/`removed` reconciliation is designed for exactly this "the local state is stale" situation.
8. Write down what happened and why, per `docs/incident-response.md`'s "after the incident" section, even if this wasn't triggered by a Class C incident.

## What this does not cover

Restoring to a *different* PostgreSQL major version than the backup was taken from, or restoring across an off-machine storage transfer — both depend on decisions (see "Deliberately deferred" in `docs/backups.md`) that haven't been made yet, since no off-machine backup destination exists.
