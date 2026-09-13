# Backups

Milestone 7 deliverable (handoff §22, ADR-015). "A backup that has never been restored is not verified" — this document, `src/finance_app/ops/backup.py`, and the `finance-restore-drill.timer` exist to make that claim checkable, not assumed. See `docs/runbooks/restore-backup.md` for the step-by-step owner procedure and `docs/adr/ADR-015-backup-encryption-gpg-symmetric.md` for why GPG symmetric encryption was chosen.

## Pipeline

```
pg_dump (as finance_backup — read-only everywhere, migrations/versions/0002)
    -> plaintext .dump file, custom format, private temp location on the backup volume
    -> gpg --symmetric --cipher-algo AES256 (passphrase via stdin, never argv — ADR-015)
    -> plaintext deleted immediately
    -> sha256 + size computed
    -> ops.backup_runs row recorded (as finance_app; finance_backup cannot write)
```

All of this is `src/finance_app/ops/backup.py::create_backup()` — not shell one-liners — so it's unit-testable and there is exactly one place this logic lives. `deploy/scripts/backup.sh` is a two-line wrapper that runs it inside the already-built image's dedicated one-shot `backup` Compose service via `docker compose --profile backup run --rm -T backup python -m finance_app.ops.backup backup` — never the long-running `app` service, so `finance_backup`/`BACKUP_ENCRYPTION_KEY` stay scoped to that one-shot job (ADR-016 D1) — so a backup always uses the exact code of the currently-deployed release.

## Schedule and retention

| Job | Cadence | Unit |
|---|---|---|
| Backup | Daily, 03:00 (before the 06:00 sync) | `finance-backup.timer` |
| Restore-verification drill | Weekly, Sunday 04:00 | `finance-restore-drill.timer` |

Retention/off-machine transfer is intentionally **not automated by this milestone** — see "Deliberately deferred" below. `BACKUP_DIR` (default `/var/lib/finance-app/backups` in production, a Docker volume) is where encrypted archives accumulate; the owner's runbook step for pruning/shipping them off-host is documented, not scripted, until a concrete off-machine target is chosen.

## Restore-verification drill

`deploy/scripts/restore-verify.sh`, run weekly by `finance-restore-drill.timer`:

1. Look up the most recent successful backup (`python -m finance_app.ops.backup latest`).
2. Start a **throwaway** `postgres:17-alpine` container on the same Docker network as the production stack (`finance-app`) — never the real `postgres` service, never even the dev database.
3. Restore the backup into it (`python -m finance_app.ops.backup restore`).
4. Run `finance_app.ops.backup`'s post-restore sanity checks against the restored database (`python -m finance_app.ops.backup verify`): `alembic_version` is present and has a value, and row counts for a representative table from every schema (`plaid.items`, `plaid.accounts`, `plaid.transactions`, `user.preferences`, `finance.budgets`, `agent.tool_calls`) match what was recorded at backup time.
5. Record a `restore_verification` row in `ops.backup_runs`, linked to the backup it verified (`verifies_backup_id`) — written against the *production* database via `finance_app`, not the throwaway one, so `finops backup-status` sees it.
6. Always tear the throwaway container down, success or failure (`trap cleanup EXIT INT TERM`).

This is a full, real restore into a real (if disposable) PostgreSQL instance — not a checksum comparison, not a "the file exists" check.

## `finops backup-status`

```
finops backup-status
```

Reports `verified` only if the most recent backup *and* a successful restore-verification of that specific backup both exist, and the verification is no more than 14 days old (`_STALE_BACKUP_VERIFICATION_DAYS`, `src/finance_app/ops/status.py`). A backup that succeeded but was never (or not recently) restore-verified reports `unverified` — the command exits non-zero in that case, so it's usable as a scripted gate. This is deliberately stricter than "did `pg_dump` exit 0."

## RPO / RTO (single-user personal finance system)

- **RPO (recovery point objective): ~24 hours.** One backup per day; worst case is losing up to a day of Plaid sync/agent-metadata changes. Plaid transaction history itself is re-fetchable via a fresh `/transactions/sync` from cursor `None` if the database is lost entirely — the backup's job is metadata (budgets, categories, notes, conversation/audit history) that has no other source of truth, plus avoiding a slow full re-sync.
- **RTO (recovery time objective): under an hour**, dominated by `pg_restore` time on a single-user-sized database and the owner running the runbook manually — there is no automated failover, which is an appropriate amount of ceremony for a single-user system (CLAUDE.md: don't add infrastructure without a demonstrated need).

These are deliberately loose relative to a multi-user production system — a single owner's personal finance data, not a service with an SLA.

## What's encrypted, and when

The plaintext `pg_dump`/`pg_restore` output exists only briefly, on local disk, inside the `backup` container's filesystem (the mounted backup volume), in a private per-run staging directory — removed immediately after GPG encryption succeeds, including in the failure path (`finally: shutil.rmtree(staging_dir, ignore_errors=True)` in `create_backup`/`restore_backup`, QA-10). A leftover staging directory from a process that didn't get to run its own `finally` (SIGKILL, OOM) is swept on the next backup/restore invocation (`_sweep_stale_staging_dirs`). The encrypted `.gpg` archive is the only artifact that persists.

## Deliberately deferred

- **Off-machine / cloud backup storage.** This milestone proves the backup/restore/verify *pipeline* end to end against the local dev Postgres container with synthetic data (satisfying this milestone's "backup restore test succeeds" exit criterion) and documents exactly what an off-machine target needs to satisfy: read/write access to `BACKUP_DIR`'s contents (or a hook in `deploy/scripts/backup.sh` to push the just-created archive after `finance_app.ops.backup.create_backup()` returns), and its own credential delivered the same way as every other secret — a systemd encrypted credential, never a plaintext key file. No cloud storage credentials are readable by the engineering session (ADR-010, revised 2026-09-13 for the shared-VPS boundary) and none were fabricated to simulate one; provisioning a real off-machine destination (object storage, a second host, etc.) is an owner decision and an owner-performed runbook step once a target is chosen.
- **Retention/pruning policy automation.** Old archives are not automatically deleted. Until an off-machine target exists, this is deliberate — deleting the only copy of an old backup before it's shipped anywhere else would be actively harmful.
- **Docker secrets-in-env architectural gap (`BACKUP_ENCRYPTION_KEY`, `FINANCE_BACKUP_DB_PASSWORD`, and every other production credential).** `with-production-env.sh` decrypts each systemd `LoadCredentialEncrypted=` value and exports it as a plain process environment variable, which `docker compose` then interpolates into the `backup`/`app`/etc. services' `environment:` blocks. That means inside a running container these secrets live as ordinary env vars — readable via `docker inspect`, `/proc/<pid>/environ`, or a careless `docker compose config` — for as long as the container runs, not just for the moment `with-production-env.sh` reads them. The fix is Docker-native file-based secrets (`secrets:` top-level key, bind-mounting `$CREDENTIALS_DIRECTORY`'s files straight into each container at `/run/secrets/*` and having the app read `*_FILE` paths instead of `*` env vars) so a secret is never a process environment variable inside the container at all. Not implemented in this pass — it's a topology change touching every service definition in `deploy/compose.yaml` plus `finance_app.config.settings`'s credential-loading path, and needs its own review rather than being folded into an unrelated bug-fix round. See `docs/security-model.md` invariant 4 for the full writeup.
