# Runbook: provisioning the VPS, first deploy, normal release, rollback, credential rotation

Owner-performed. The Claude Code engineering environment never performs any step in this document beyond the earlier `git push`/CI stages — it has no VPS credentials, no SSH access, and per ADR-010 must never be given any. Everything below happens on the VPS itself, over the owner's own SSH session. See `docs/deployment.md` for the release-sequence design this runbook implements and `docs/security-model.md` for why the boundary is drawn here.

## 1. Provision the VPS (one time)

1. Provision a Linux VPS (any provider; systemd + Docker is the only requirement — `docs/architecture.md`'s "deliberate omissions" apply here too, no managed Kubernetes).
2. Install Docker Engine + the Compose plugin (`docker compose version` should report v2).
3. Confirm `systemd-creds` is available (`systemd-creds --version`; systemd ≥ 250 — check the distribution's systemd version before provisioning if unsure).
4. Create the deploy directory and restrict it:
   ```
   sudo mkdir -p /opt/finance-app /etc/finance-app/credentials
   sudo chmod 700 /etc/finance-app/credentials
   ```
5. Copy `deploy/` (this repository's `deploy/compose.yaml`, `deploy/caddy/`, `deploy/scripts/`) to `/opt/finance-app/deploy/` and the unit files in `deploy/systemd/*.service`/`*.timer` to `/etc/systemd/system/`. This is a deliberate, owner-performed sync — not something CI pushes automatically (ADR-007: no self-hosted runner, no arbitrary PR code execution on the VPS). Re-run this step only when `deploy/` itself changes, which should be rare; ordinary application releases never touch these files, only the image tag (`RELEASE_ID`).
6. `sudo systemctl daemon-reload`

## 2. Mint the production credentials (one time, then per rotation)

Each of the following is minted with `systemd-creds encrypt`, which binds the encrypted credential to this specific host's TPM/machine key (`man systemd-creds`) — a `.cred` file copied to a different host will not decrypt there, which is intentional.

```
echo -n '<value>' | sudo systemd-creds encrypt - /etc/finance-app/credentials/<name>.cred
```

Required credentials (`<name>` matches the systemd unit files' `LoadCredentialEncrypted=name:path` and `deploy/scripts/with-production-env.sh`'s mapping — do not rename one side without the other):

| `<name>` | Source of the value |
|---|---|
| `plaid_client_id` | Plaid dashboard, production credentials |
| `plaid_secret` | Plaid dashboard, production credentials |
| `plaid_access_token` | `docs/runbooks/plaid-link.md`'s production Link procedure |
| `plaid_webhook_secret` | Milestone 8 — leave unset until the webhook endpoint exists |
| `openai_api_key` / `anthropic_api_key` | Whichever `AGENT_PROVIDER` is active (ADR-014) — only that one is required |
| `finance_migrator_db_password`, `finance_app_db_password`, `finance_agent_db_password`, `finance_observer_db_password`, `finance_backup_db_password` | Generate independently per role, e.g. `openssl rand -base64 32` each — never reuse the dev `devpassword` in production |
| `backup_encryption_key` | `openssl rand -base64 32` — see ADR-015. **Losing this makes every existing backup unrecoverable; store an out-of-band copy somewhere durable before proceeding (a password manager, not this repository, not the VPS itself).** |

Also create `/etc/finance-app/env` (plain file, non-secret configuration only — never a credential):

```
CONTAINER_IMAGE_REPO=ghcr.io/jq712/claude-finance-app
AGENT_PROVIDER=openai
LOG_LEVEL=INFO
```

## 3. First deploy

1. Confirm CI is green on `main` and note the commit SHA you intend to deploy (`git log -1 --format=%H`, or read it off the `production-deploy` job's step summary for that push — it publishes readiness for exactly this SHA).
2. Enable and start the always-on unit and the timers:
   ```
   sudo systemctl enable --now finance-app.service
   sudo systemctl enable --now finance-sync.timer finance-backup.timer finance-health.timer finance-restore-drill.timer
   ```
3. Run the migration and deploy:
   ```
   cd /opt/finance-app
   deploy/scripts/with-production-env.sh deploy/scripts/finops.sh deploy <sha>
   ```
   `finops deploy` runs inside the narrowly-scoped `deploy` Compose service (Docker socket mounted — see that service's comment in `compose.yaml` for why it's split from the long-running `app` service). It pulls the image, brings the stack up under that tag, runs the health check, and either promotes the release to `current` or automatically rolls back — see `docs/deployment.md`.
4. Verify:
   ```
   docker compose -f deploy/compose.yaml run --rm app finops health
   docker compose -f deploy/compose.yaml run --rm app finops version
   ```
   (These two are plain reads through `finance_observer` — no socket access needed, so they run in the ordinary `app` service, not `deploy`.)

## 4. Normal release (every subsequent deploy)

Once CI has published a new image and the `production` GitHub Environment approval has been granted for that SHA (`docs/deployment.md`):

```
cd /opt/finance-app
deploy/scripts/with-production-env.sh deploy/scripts/finops.sh deploy <sha>
```

That's the entire procedure — no compose file edits, no manual image pulls, no restart choreography. `finops deploy` handles pull, bring-up, health verification, and (on failure) automatic rollback.

## 5. Rollback

```
deploy/scripts/with-production-env.sh deploy/scripts/finops.sh rollback
```

Rolls back to the tracked previous known-good release — no rebuild, no registry fetch beyond what's already local (ADR-008). If `finops rollback` reports no previous release is tracked (e.g. this was the very first deploy), there is nothing to roll back to; fix forward instead.

## 6. Restart (no release change)

```
deploy/scripts/with-production-env.sh deploy/scripts/finops.sh restart
```

Restarts only the `app` container — does not touch Postgres, does not change `ops.releases`. Use this when the app process itself needs to come back (e.g. after a transient resource issue), not as a substitute for a release.

## 7. Credential rotation

1. Mint the new value (§2's `systemd-creds encrypt` command) — this overwrites the `.cred` file in place.
2. Restart whichever unit(s) load that credential so the new value takes effect:
   ```
   sudo systemctl restart finance-app.service
   ```
   (`finance-sync`/`finance-backup`/`finance-health`/`finance-restore-drill` are `Type=oneshot` timers — they pick up the new credential automatically on their next scheduled run; no restart needed for those.)
3. For a **database role password**, the role's actual PostgreSQL password must also be changed (`ALTER ROLE ... PASSWORD ...`, via `finance_migrator` — this is a DDL-adjacent action outside `finops`'s normal surface and is itself a Class B change; treat it with the same care as a migration) before the new credential value will actually authenticate.
4. For the **Plaid access token**, see `docs/runbooks/plaid-link.md`'s rotation/re-authentication section — the systemd credential is only the storage half of that procedure.
5. Verify with `finops health` / `finops db-status` after rotating.

## 8. Configuring the `production` GitHub Environment approval gate

One-time, in the GitHub repository: **Settings → Environments → New environment → `production`** → add required reviewers. Until this is configured, `.github/workflows/ci.yml`'s `production-deploy` job runs unattended (harmless — it's a no-op summary, not an actual deploy) but should be locked down before treating it as a meaningful gate.
