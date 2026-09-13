# Runbook: provisioning `/opt/finance`, first deploy, normal release, rollback, credential rotation

Owner-performed. **Rewritten 2026-09-13 for ADR-019** (bare-metal, no Docker) — supersedes this runbook's previous Docker Compose procedure. The engineering workspace and the production deployment share one VPS (ADR-007/ADR-010, revised the same date), but the Claude Code engineering session never performs any step in this document: it cannot read `/opt/finance` or its `.env`, and has no `sudo` — not because it lacks a network path (it's on the same box), but because `/opt/finance` is owned by a separate, more-privileged Unix user it is not a member of. Everything below happens as the owner, either at the console or over SSH into that same VPS. See `docs/deployment.md` for the release-sequence design this runbook implements and `docs/security-model.md` for the boundary this depends on.

**Status as of 2026-09-13: none of this has been carried out yet.** No `/opt/finance`, no production Unix user, no `finance_prod` database exist on the VPS. This is the target procedure (ADR-019); a follow-up session builds the scripts this runbook references before it can actually be followed end to end.

## 1. Provision the host (one time)

1. Confirm PostgreSQL is installed and running on the VPS (host-installed, not a container). Create the `finance_prod` database alongside the existing `finance_dev` one, on the same PostgreSQL instance — see `docs/database.md` for the role/grant structure both databases share.
2. **Create the production Unix user and lock down `/opt/finance`** — this is the boundary everything else in this runbook and in `docs/security-model.md` depends on:
   ```
   sudo useradd --system --create-home --home-dir /opt/finance --shell /usr/sbin/nologin finance-prod
   sudo mkdir -p /opt/finance/releases
   sudo chown -R finance-prod:finance-prod /opt/finance
   ```
   Confirm the engineering session's own Unix user (whatever user Claude Code sessions run as) is **not** a member of `finance-prod`'s group and cannot read `/opt/finance` — `sudo -u <engineering-user> ls /opt/finance` should fail with permission denied. Confirm that user also has no `sudo` entry and is not in the `systemd-journal` group (`docs/incident-response.md`). Re-run these checks periodically — permission drift here is a silent, total loss of the confidentiality boundary this whole setup relies on.
3. Confirm `systemd-creds` is available if any credential will use it (`systemd-creds --version`; systemd ≥ 250) — optional hardening for a particularly sensitive value (the Plaid access token, the backup encryption key); the primary credential store is `/opt/finance/.env` below.
4. `sudo systemctl daemon-reload` once the unit files (step 5 below) are in place.
5. Copy the systemd unit files this repository defines for production processes/timers to `/etc/systemd/system/` — a deliberate, owner-performed sync, not something CI pushes automatically (ADR-007: no arbitrary PR code execution against `/opt/finance`). Re-run this step only when the unit definitions themselves change; ordinary application releases only change which release directory `current` points at.

## 2. Create the production environment file (one time, then per rotation)

**`/opt/finance/.env`, mode 600, owned by `finance-prod`** — the primary production configuration and credential store, analogous to this repository's own `.env`/`.env.dev` but pointing at `finance_prod` with real credentials:

```
sudo -u finance-prod touch /opt/finance/.env
sudo chmod 600 /opt/finance/.env
```

Populate it (as `finance-prod`, or via `sudo -u finance-prod $EDITOR /opt/finance/.env` — never typed where the engineering session's user could read the resulting file or shell history) with:

| Variable | Source of the value |
|---|---|
| `PLAID_CLIENT_ID` / `PLAID_SECRET` | Plaid dashboard, production credentials |
| `PLAID_ACCESS_TOKEN` | `docs/runbooks/plaid-link.md`'s production Link procedure |
| `PLAID_WEBHOOK_SECRET` | Milestone 8 — leave unset until the webhook endpoint exists |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Whichever `AGENT_PROVIDER` is active (ADR-014) — only that one is required |
| `DATABASE_URL`, `ALEMBIC_DATABASE_URL`, `AGENT_DATABASE_URL`, `OBSERVER_DATABASE_URL`, `BACKUP_DATABASE_URL` | Point at `finance_prod` on the host PostgreSQL instance, one DSN per role (`docs/database.md`) — generate each role's production password independently, e.g. `openssl rand -base64 32`; never reuse the dev `devpassword` |
| `BACKUP_ENCRYPTION_KEY` | `openssl rand -base64 32` — see ADR-015. **Losing this makes every existing backup unrecoverable; store an out-of-band copy somewhere durable before proceeding (a password manager, not this repository, not the VPS itself).** |
| `AGENT_PROVIDER`, `LOG_LEVEL` | Plain configuration, e.g. `openai`, `INFO` |

A particularly sensitive value (the Plaid access token, the backup encryption key) can additionally go through `systemd-creds encrypt` and a unit's `LoadCredentialEncrypted=` instead of sitting in this file, if the follow-up implementation decides that extra layer is worth it for that value specifically — not required by this design the way the old per-job Compose credential matrix was, since there's no more whole-file Compose interpolation problem forcing that split.

## 3. First deploy

1. Confirm CI is green on `main` and note the commit SHA you intend to deploy (`git log -1 --format=%H`).
2. Copy that git ref into a new release directory: `/opt/finance/releases/<sha>/` — as `finance-prod` (e.g. `sudo -u finance-prod git archive <sha> | sudo -u finance-prod tar -x -C /opt/finance/releases/<sha>`, or equivalent; the exact mechanism is a follow-up implementation decision, but it must run as `finance-prod`, never as the engineering session's user).
3. Build the release's virtualenv: `cd /opt/finance/releases/<sha> && sudo -u finance-prod uv sync --locked --no-dev` (or however the follow-up session wires this).
4. Back up `finance_prod` before touching it (`docs/backups.md`'s procedure, adapted to run directly against the host database).
5. Run the migration: `alembic upgrade head` from the new release directory, using `/opt/finance/.env`'s `ALEMBIC_DATABASE_URL`.
6. Point `current` at the new release: `sudo -u finance-prod ln -sfn /opt/finance/releases/<sha> /opt/finance/current`.
7. `sudo systemctl enable --now` the production units and timers (names TBD by the follow-up session's unit files — analogous to the old `finance-app.service`/`finance-sync.timer`/`finance-backup.timer`/`finance-health.timer`/`finance-restore-drill.timer`, now invoking `/opt/finance/current/.venv/bin/finance`/`finops` directly instead of `docker compose run`).
8. Verify: `finops health` / `finops version`, run as `finance-prod` (or via whatever wrapper the follow-up session provides) against `/opt/finance/.env`.

## 4. Normal release (every subsequent deploy)

Repeat steps 2–8 of §3 for the new SHA. The `current` repoint (step 6) plus a restart (step 7) is the entire "promote" action — no compose file edits, no image pulls, no registry.

## 5. Rollback

Repoint `current` at the previous release directory and restart:

```
sudo -u finance-prod ln -sfn /opt/finance/releases/<previous-sha> /opt/finance/current
sudo systemctl restart <production units>
```

ADR-008's principle, carried into ADR-019: exactly one rollback step is guaranteed trivial, and it's simpler here than under the Docker design — no registry fetch at all, since the previous release's files are already on disk in `/opt/finance/releases/`. If there is no previous release directory (this was the very first deploy), there is nothing to roll back to; fix forward instead. `finops rollback` should eventually automate this symlink-and-restart sequence with the same health-check-before-promoting discipline `probe_release` provided under the Docker design (follow-up implementation).

## 6. Restart (no release change)

```
sudo systemctl restart <production units>
```

Re-confirms the currently-`current` release is healthy without changing what's deployed. `finops restart` should wrap this with a selfcheck, same as before, once implemented.

## 7. Credential rotation

1. Edit the value directly in `/opt/finance/.env` (or re-run `systemd-creds encrypt` for a value using that path) — as `finance-prod`, never from the engineering session.
2. Restart whichever production unit(s) read that value so the new one takes effect.
3. For a **database role password**, the role's actual PostgreSQL password must also be changed (`ALTER ROLE ... PASSWORD ...`, via `finance_migrator`, against `finance_prod` — this is a DDL-adjacent action outside `finops`'s normal surface and is itself a Class B change; treat it with the same care as a migration) before the new value in `.env` will actually authenticate.
4. For the **Plaid access token**, see `docs/runbooks/plaid-link.md`'s rotation/re-authentication section — `.env` (or a `systemd-creds` credential, if that value uses one) is only the storage half of that procedure.
5. Verify with `finops health` / `finops db-status` after rotating.

## 8. What's no longer needed

No CI image-publish step, no `production` GitHub Environment approval gate over an image tag, no container registry credentials. A follow-up session decides whether any equivalent "is this SHA safe to copy to `/opt/finance`" gate belongs in CI at all, now that there's no artifact for such a gate to attach to.
