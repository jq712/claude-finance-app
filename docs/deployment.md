# Deployment

Milestone 7 deliverable (handoff §29). Covers the production topology, the release/rollback sequence, and how CI/CD, `finops`, and systemd fit together. **Rewritten 2026-09-13 for ADR-019** (bare-metal, no Docker, `/opt/finance`) — supersedes the Docker Compose design ADR-016 described; see that ADR's own superseded note and ADR-019 for the full decision and its implementation-status table. See `docs/architecture.md` for the system-wide picture, `docs/security-model.md` for the invariants this design enforces, `docs/backups.md` for backup/restore, and `docs/runbooks/deploy.md` for the exact owner-performed procedure.

## What exists today vs. what this document describes

This document describes the **target** architecture (ADR-019). As of 2026-09-13, **none of the bare-metal release mechanism is built yet** — `finops deploy`/`rollback`/`restart` and `src/finance_app/ops/compose.py` still implement the superseded Docker Compose design end to end, and CI's `container-build`/`publish-image`/`migration-preflight`/`staging-smoke` jobs still build and push a Docker image. This is a documentation-only pass; a follow-up session implements the rewrite described below. Do not treat any Docker reference still live in the codebase as current guidance — it's the thing being replaced, not an alternative design. **No production deployment is provisioned yet** either way: `/opt/finance` doesn't exist on the VPS, and neither does the second `finance_prod` database.

## Topology

```
Dev tree (this repository)     /opt/finance (production, bare metal)
  finance_dev on the host        finance_prod on the same host
  PostgreSQL instance             PostgreSQL instance
  .env / .env.dev                 releases/<sha>/, current -> releases/<sha>
  Claude Code operates here       systemd units invoke current/.venv/bin/*
                                   Claude Code never edits this directory
```

One host PostgreSQL server, two logical databases (`finance_dev`, `finance_prod`) — not two installs, not containers. `/opt/finance` is release directories plus a `current` symlink (the standard bare-metal release convention: rollback is a symlink repoint, not a rebuild), owned by a dedicated production Unix user the engineering session's user cannot read or write as (`docs/runbooks/deploy.md` §1, `docs/security-model.md`'s "Trust boundaries"). No image, no registry, no container runtime anywhere in this application's own stack (CI's use of a Postgres *service container* to provision a throwaway test database for `pytest` is GitHub Actions' own test infrastructure and is unrelated).

Credentials live in `/opt/finance/.env` (mode 600, production Unix user only) plus systemd encrypted credentials for anything a unit needs decrypted at process start — never a plaintext file this repository can read, never baked into anything, exactly as before, just without a container boundary in the story.

## Release sequence (ADR-007, ADR-019)

```
push to main
  -> lint/typecheck, unit, integration, security, agent-evals, secret-scan   (existing CI)
  -> [owner]              copy a known, CI-green git ref into
                           /opt/finance/releases/<sha>/
  -> [owner]              back up finance_prod
  -> [owner]              alembic upgrade head against finance_prod,
                           from the new release directory
  -> [owner]              repoint /opt/finance/current -> releases/<sha>
  -> [owner]              systemctl restart the production units
  -> [owner]              finops health / finance status against finance_prod
                           to confirm; repoint `current` back and restart
                           again if unhealthy (ADR-008's "one rollback step"
                           principle, now a symlink swap)
  -> release recorded (mechanism TBD by the follow-up session — some
     equivalent of today's ops.releases bookkeeping, against finance_prod)
```

No image build, no registry, no `production-deploy` GitHub Environment gate in the old sense — a follow-up session decides what (if anything) CI's own gating looks like for "this git ref is safe to copy to `/opt/finance`" once the release mechanism itself is designed. Until then, treat every push to `main` that's green through the existing test/lint/security/eval jobs as a candidate the owner may deploy by hand, per the runbook.

## `finops deploy` / `rollback` / `restart` — target design

These three commands stay the *only* place `finops` changes production state (every other command — `health`, `sync-status`, `db-status`, `migration-status`, `backup-status`, `recent-errors` — stays a read-only query through `finance_observer`). What changes is what they actually do:

- **`finops deploy <sha>`** — validates `<sha>` looks like a Git SHA, records a release-tracking row, copies/confirms the release directory exists at `/opt/finance/releases/<sha>/` (or expects the owner to have already done so — exact division of labor between the owner's copy step and what `finops deploy` itself automates is a follow-up design decision), runs the migration preflight against `finance_prod`, health-checks via the release directory's own `finance selfcheck`, and on success repoints `current` and restarts the production systemd units. Unhealthy → automatic rollback to the previous release directory (ADR-008's principle, unchanged).
- **`finops rollback`** — repoints `current` back to the tracked previous release directory and restarts, after confirming that release still selfchecks healthy. No rebuild, nothing to pull — even more literally "no registry fetch beyond what's already local" than the Docker design, since there's no registry at all.
- **`finops restart`** — re-runs the health selfcheck against whichever release `current` points at and restarts the production units if needed. Never changes `current`.

None of them accept a SQL string, a shell string, or an SSH target — the command surface stays fixed (handoff §10's "no arbitrary SQL/shell" applies to deployment exactly as it applies to diagnosis). What they shell out to changes from `docker compose` (`src/finance_app/ops/compose.py`) to whatever direct-process/systemd invocation the follow-up session designs — the bookkeeping module (`src/finance_app/ops/release.py`) needs no conceptual change, since it never depended on Docker in the first place (it only tracks release identifiers and current/previous/failed/rolled_back status).

## Health checks and automatic rollback

Unchanged in design from before — `finops health` (read-only, `finance_observer`) aggregates database reachability, migration-head match, sync freshness, and backup verification freshness exactly as it did under the Docker design; `finance-health.timer` still runs the same aggregate check every 15 minutes via the `finance_app` role. None of this was ever Docker-specific.

## Rollback semantics

ADR-008's principle, carried into ADR-019 exactly as stated there: exactly one rollback step is guaranteed trivial — current ↔ previous, now a `current` symlink repoint instead of an image tag swap. `ops.releases` (or its bare-metal equivalent) tracks at most one `current` row and at most one `previous` row; anything older is left as plain history. If a second consecutive deploy also fails, `finops rollback` still returns to the last-known-good release, because a failed deploy attempt never touches the `current`/`previous` bookkeeping.

The `wrong_image`/build-time-identity verification ADR-016 D3 added (`IMAGE_RELEASE_ID` baked at `docker build` time) has no equivalent need under ADR-019: there is no image to mistake for another. The health check that matters is the same one — does the release directory's own `finance selfcheck` report itself healthy at the expected migration head — without a separate "is this actually the release I asked for" layer, since a release directory's contents are exactly what was copied there, not something a registry pull could silently substitute.

## Deliberately deferred

- **The entire bare-metal release mechanism** (ADR-019) — `/opt/finance` provisioning, the release-copy script, `finops deploy`/`rollback`/`restart` rewritten, CI's Docker-shaped jobs redesigned, Settings/CLI's explicit-prod-opt-in. See ADR-019's implementation-status table for the full list; this is a follow-up session's work, not done in this documentation pass.
- **Real production provisioning on the shared VPS** — the engineering workspace's own host is the intended production host (ADR-007/ADR-010/ADR-019), but `/opt/finance`, its Unix user, and `finance_prod` don't exist yet. `docs/runbooks/deploy.md` documents the procedure.
- **Milestone 8's webhook endpoint** — was scaffolded via a Compose `caddy` service under the old design; a follow-up session needs a bare-metal equivalent (a host-installed reverse proxy, or Python serving TLS directly) once this milestone's rewrite lands.
- **A "canary"/gradual rollout mechanism** — out of scope for a single-user, single-instance application; all-or-nothing health-gated promotion is the appropriate amount of ceremony here (CLAUDE.md: don't add complexity without a demonstrated need).
