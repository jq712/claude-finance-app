# Deployment

Milestone 7 deliverable (handoff §29). Covers the production topology, the release/rollback sequence, and how CI/CD, `finops`, and systemd fit together. See `docs/architecture.md` for the system-wide picture, `docs/security-model.md` for the invariants this design enforces, `docs/backups.md` for backup/restore, and `docs/runbooks/deploy.md` for the exact owner-performed procedure (VPS provisioning, minting credentials, first deploy, rotation).

## What exists today

Everything up to "an owner-performed action once the VPS is provisioned": the Docker image, the production Compose file, systemd units, the CI/CD pipeline through image publish/migration-preflight/staging-smoke, and the `finops deploy`/`rollback`/`restart` commands that will operate on the real VPS once it exists. **No production VPS is provisioned yet.** `.github/workflows/ci.yml`'s `production-deploy` job is a deliberate no-op gate that records release readiness in the job summary — it does not SSH anywhere or fake a deploy target, per ADR-007/ADR-010. The first real deploy is a manual, owner-performed run of `finops deploy <sha>` on the VPS, documented step by step in `docs/runbooks/deploy.md`.

## Topology

```
app        finance-app image (Dockerfile) — finance/finops entrypoints,
           plus deploy/scripts/*.sh's pg_dump/pg_restore/gpg dependencies.
           Not long-running until Milestone 8 (ADR-016 D2): profile-gated,
           started only as one-shot `docker compose --profile app run
           --rm app <cmd>` (the post-deploy `finance selfcheck` probe,
           interactive `finance chat`) — never `up -d`, never `restart:`.
postgres   postgres:17-alpine, not publicly exposed
caddy      reverse proxy for the future Plaid webhook (Milestone 8) —
           behind the `webhook` Compose profile, not started by default
```

`migrate`, `sync`, `backup`, `finops`, and `deploy` are further one-shot, profile-gated services splitting out the credentials/authority each job actually needs (ADR-016 D1) — see `deploy/compose.yaml`'s own header comment for the full per-service breakdown.

`deploy/compose.yaml` is the production Compose file (`deploy/compose.dev.yaml` stays the disposable dev/CI Postgres-only container — don't confuse the two). No secret is ever baked into the image or the Compose file. Credential-shaped environment variables are **not** enforced required by Compose itself (`${VAR:-}` everywhere, ADR-016 D1 — Compose interpolates the whole file regardless of which service is selected, so a whole-file `${VAR:?...}` can't express a per-job requirement); `deploy/scripts/with-production-env.sh` enforces per-job presence instead, before `docker compose` ever runs, and a drift test keeps its job matrix in sync with this file. Every credential is supplied at container start from systemd encrypted credentials (ADR-010), never a plaintext `.env` in production.

## Release sequence (ADR-007, ADR-008)

```
push to main
  -> lint/typecheck, unit, integration, security, agent-evals, secret-scan, container-build   (existing CI, extended)
  -> publish-image        build + tag by Git SHA + push to ghcr.io/<owner>/<repo>
  -> migration-preflight  `alembic upgrade head` from inside the packaged image, fresh DB
  -> staging-smoke        deploy/compose.yaml brought up on the runner with the new image;
                          finops health / finance status must succeed
  -> production-deploy    gated by the `production` GitHub Environment (manual approval) —
                          currently a no-op that records readiness; see "What exists today"
  -> [owner, on the VPS]  finops deploy <sha>
  -> post-deploy health verification, automatic rollback on failure (finops deploy itself)
  -> release recorded in ops.releases
```

Every job through `staging-smoke` runs on every push to `main` unconditionally. `production-deploy` requires the `production` GitHub Environment's required reviewers to approve — configure that once in **repo Settings → Environments → production** (`docs/runbooks/deploy.md`); until it's configured, that job runs unattended, which is harmless pre-VPS but must be set before a real deploy depends on it.

## `finops deploy` / `rollback` / `restart`

These three commands are the *only* place `finops` changes production state (every other command that touches the database — `health`, `sync-status`, `db-status`, `migration-status`, `backup-status`, `recent-errors` — is a read-only query through the `finance_observer` role; `version` doesn't connect to a database at all, it just prints the in-process version string and the `RELEASE_ID` env var). They are meant to run **on the VPS**, operating on the Compose stack already running there:

- **`finops deploy <sha>`** — validates `<sha>` looks like a Git SHA (refuses anything else; `latest` is never accepted here even though the image carries that tag too), records a `ops.releases` row (`status="pending"`), runs `docker compose pull` and the migration preflight with `RELEASE_ID=<sha>`, then health-checks the release via `deploy_health_check` (ADR-016 D3: wraps `probe_release`, a one-shot `docker compose --profile app run --rm app finance selfcheck --json` against the exact image being deployed, pinned to `<sha>` — not a persistent `app` container, which does not exist until Milestone 8's webhook server; there is no `up -d`/bring-up step). Healthy and reporting the right release id → promoted to `current` (the prior `current` becomes `previous`). Unhealthy, unreachable, or reporting the wrong release id → marked `failed` and **`finops deploy` automatically rolls back** to the previous known-good release (ADR-008) before exiting non-zero.
- **`finops rollback`** — the same rollback mechanics, invokable directly: confirms the tracked `previous` release's image still selfchecks healthy (`probe_release`, no rebuild, no registry fetch beyond what's already local, and — like `deploy` — no `up -d`/bring-up step) and, only if healthy, flips `ops.releases` bookkeeping back to it. Refuses (raises rather than promoting) if that target itself fails its own selfcheck.
- **`finops restart`** — ADR-016 D2: `app` is a one-shot command until Milestone 8, not a long-running process, so there is nothing to restart; this instead reads the currently-recorded `current` release id (`finance_observer`, read-only) and re-runs `probe_release` against it, reporting whether it's still healthy. Never writes to `ops.releases`.

All three shell out to `docker compose` via `src/finance_app/ops/compose.py`, with the actual subprocess call injectable so the bookkeeping (`src/finance_app/ops/release.py`) is unit-testable without a Docker daemon. None of them accept a SQL string, a shell string, or an SSH target — the command surface is fixed (handoff §10's "no arbitrary SQL/shell" applies to deployment exactly as it applies to diagnosis).

## Health checks and automatic rollback

`finops health` (read-only, `finance_observer`) aggregates:

- **database** — reachable
- **migrations** — the DB's applied Alembic revision matches the repo's head revision
- **sync** — the most recent `ops.sync_runs` row isn't `error` and isn't more than ~36h old
- **backup** — the most recent backup has a successful restore-verification within the last 14 days

`overall` is `healthy` only if database/migrations are fine and sync isn't `error`/`stale` (a fresh install with no sync yet is not treated as unhealthy — `never_run` is a distinct state). `finops deploy` calls this exact function immediately after bringing the new release up; a non-`healthy` result is what triggers the automatic rollback described above.

Separately, `finance-health.timer` runs `python -m finance_app.ops.health` every 15 minutes — the same aggregate check, but via the `finance_app` role so it can additionally write an `ops.errors` row when unhealthy (see `src/finance_app/ops/health.py`'s module docstring for why that path uses a different role than the `finops health` CLI command).

## Rollback semantics

ADR-008: exactly one rollback step is guaranteed trivial — current ↔ previous. `ops.releases` tracks at most one `current` row and at most one `previous` row (`src/finance_app/ops/release.py`); anything older is left as plain history, not an arbitrary-depth undo stack. If a second consecutive deploy also fails, `finops rollback` still returns to the last-known-good release, because `mark_failed` never touches the `current`/`previous` bookkeeping — only `mark_healthy` does.

`probe_release`'s `wrong_image` check (ADR-016 D3) compares the image's build-time-baked identity — `IMAGE_RELEASE_ID`, set once at `docker build` time from CI's `RELEASE_ID=<sha>` build-arg — against the release id requested, never the runtime `RELEASE_ID` environment variable the check itself injects. **One-time migration note:** any release image built before this check existed has no baked identity, so `probe_release` reports it `wrong_image` and `finops rollback`/`restart` refuse it with "manual intervention required" — a genuine, if narrow, gap in "rollback is one step" for exactly the release that introduces this check. There is no legacy fallback by design (treating a missing identity as trustworthy would defeat the point of the check); the resolution is to `finops deploy <sha>` a post-change image, after which normal rollback semantics resume.

## Deliberately deferred

- **ADR-016 D8 (release-id propagation to the scheduled jobs) is not yet implemented**, despite being in scope for this milestone. `finops deploy`/`rollback` set `RELEASE_ID` only in the environment of their own `docker compose` child processes; nothing writes it back to `/etc/finance-app/env`, which is what `finance-sync.timer`, `finance-backup.timer`, `finance-health.timer`, and `finance-restore-drill.timer` read via `EnvironmentFile=`. Until this lands, those scheduled jobs resolve `${RELEASE_ID:-latest}` independently of what was actually deployed or rolled back — see ADR-016 D8 for the design and its implementation-status note for current state.
- **Real VPS provisioning** — no cloud/hosting credentials exist in this engineering environment, and per ADR-010/handoff §4.5 none should. `docs/runbooks/deploy.md` documents the exact procedure for when the owner provisions one.
- **Milestone 8's webhook endpoint** — `deploy/caddy/Caddyfile` and the `caddy` Compose service are scaffolded (`profiles: ["webhook"]`, not started by default) so Milestone 8 only has to fill in the route, not design the topology.
- **A "canary"/gradual rollout mechanism** — out of scope for a single-user, single-instance application; `finops deploy`'s all-or-nothing health-gated promotion is the appropriate amount of ceremony here (CLAUDE.md: don't add complexity without a demonstrated need).
