# Runbook: Class C — evidence preservation and escalation

For suspected credential compromise, unexplained financial data corruption, lost source-of-truth `plaid.*` records, repeated failed restores, a backup failure alongside integrity concerns, suspected unauthorized access, or a reconciliation discrepancy `finops sync-status`/`recent-errors` can't explain. See `docs/incident-response.md` for the classification framework and `docs/security-model.md` for the invariants this procedure protects.

**The correct autonomous action here is to stop and preserve, not to fix.** Do not run any of the following before completing this checklist: `finops rollback`, a migration downgrade, a manual `UPDATE`/`DELETE`, a restore over production, or a credential rotation performed before evidence is captured (rotating first can be correct once step 2 is done — order matters).

## Checklist

1. **Stop.** No destructive or automatic repair action. If a systemd timer might run again before you've finished this checklist and could make things worse (e.g. another sync while a discrepancy is under investigation), it's acceptable to pause it: `sudo systemctl stop finance-sync.timer` (a paused timer is easily resumed later and costs nothing; a corrupted-further database might not be).

2. **Preserve evidence**, in this order:
   ```
   finops recent-errors --limit 200 --json > incident-errors.json
   finops sync-status --json > incident-sync-status.json
   finops backup-status --json > incident-backup-status.json
   finops version --json > incident-version.json
   ```
   On the VPS:
   ```
   journalctl -u finance-sync -u finance-app -u finance-backup -u finance-health \
       --since "-72h" > incident-logs.txt
   ```
   Keep all of these together, timestamped, outside of anything that might get overwritten (not inside the backup volume itself).

3. **Take a safe snapshot.** An ad hoc backup right now is read-only from the database's perspective (`finance_backup` role — `SELECT` only, per `migrations/versions/0002`), so running one cannot make things worse, and it captures the current (possibly compromised/corrupted) state for later forensic comparison against earlier verified backups:
   ```
   sudo systemd-run --pty --wait --collect --same-dir \
       --property=LoadCredentialEncrypted=finance_backup_db_password:/etc/finance-app/credentials/finance_backup_db_password.cred \
       --property=LoadCredentialEncrypted=finance_app_db_password:/etc/finance-app/credentials/finance_app_db_password.cred \
       --property=LoadCredentialEncrypted=backup_encryption_key:/etc/finance-app/credentials/backup_encryption_key.cred \
       -- /opt/finance-app/deploy/scripts/with-production-env.sh backup -- \
          /opt/finance-app/deploy/scripts/backup.sh
   ```
   (`with-production-env.sh` only decrypts credentials inside a systemd
   unit declaring `LoadCredentialEncrypted=` — a bare SSH shell has no
   route to that; see `docs/runbooks/deploy.md` §2.5 for the same
   pattern used by `finops deploy`/`rollback`/`restart`.)
   Note the resulting `backup_run_id` — this snapshot is evidence, not a "known good" restore point; label it as such in the incident report.

4. **If credential compromise is suspected**, do not rotate blindly before evidence is captured — but once steps 2-3 are done, rotating is usually the right next move, since a live compromised credential is an active, ongoing risk while `plaid.*`/financial data corruption is (once snapshotted) not getting worse by itself. `docs/runbooks/deploy.md` §7 covers rotation mechanics. Rotate:
   - `plaid_access_token` if Plaid access is suspected compromised (`docs/runbooks/plaid-link.md`'s rotation section — this requires the owner to re-run Link, it cannot be done from `finops`).
   - The relevant `finance_*_db_password` if database credential compromise is suspected — this needs an `ALTER ROLE` via `finance_migrator`, done carefully (see `docs/runbooks/deploy.md` §7 step 3).
   - `openai_api_key`/`anthropic_api_key` if the runtime provider key is suspected compromised.

5. **Write the incident report.** At minimum:
   - What was observed, and when (first noticed vs. suspected actual onset).
   - Which `finops` outputs / log lines support the classification as Class C rather than B.
   - What is suspected (not confirmed) as cause.
   - What has been touched (the evidence-preservation snapshot in step 3 is expected; anything beyond that should be justified).
   - Recommended next action, with a clear "the following requires owner authorization before proceeding" list.

   Commit this to the repository (e.g. `docs/incidents/YYYY-MM-DD-<short-description>.md`, or attach to a GitHub issue) — per CLAUDE.md, this is how the incident becomes durable project memory rather than something only remembered in a chat transcript.

6. **Stop here without owner authorization.** Nothing past this point — restoring a backup over production, a migration downgrade, a manual data correction, resuming a paused timer — proceeds without the owner reviewing the incident report and explicitly authorizing the next step. This is true even if the fix seems obvious; the entire point of the Class C designation is that "obvious" fixes can destroy the evidence needed to understand what actually happened, and financial data has no do-over.

## After authorization

Once the owner has reviewed the report and authorized a specific next action (restore, rotation completion, forward-fix, etc.), follow the relevant runbook for that action (`docs/runbooks/restore-backup.md`, `docs/runbooks/deploy.md` §7) and then close the loop per `docs/incident-response.md`'s "after the incident" section — a regression test, an ADR, a `CLAUDE.md` rule, or a hook, whichever actually prevents recurrence.
