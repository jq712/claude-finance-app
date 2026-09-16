# ADR-019: Bare-metal deployment — no Docker, dev tree and production split on one shared VPS

**Status:** Accepted

## Implementation status

This ADR describes the target architecture. As of 2026-09-13 (the date of this decision), most of the *code* side of it is now built (see below); the *production provisioning* side (`/opt/finance` actually existing on the VPS) is still deliberately owner-performed and out of scope for an engineering session. Do not assume a row marked "Not shipped" exists in code just because it's described here.

| Piece | Status |
|---|---|
| Host PostgreSQL instance with `finance_dev`/`finance_prod` databases | **Shipped** — a systemd-managed PostgreSQL 17 instance (from PGDG) on port 5433, `finance_dev` migrated to head. `finance_prod` is not yet created — that's real production provisioning, owner-performed. |
| `/opt/finance` provisioned, `finance-prod` Unix user, release-directory layout | **Not shipped** — owner-performed production provisioning (`docs/runbooks/deploy.md`), out of scope for an engineering session. |
| Release-copy script (`deploy/scripts/release.sh` or equivalent) | **Shipped** (`deploy/scripts/release.sh`) — archives from a bare mirror via `git archive` (never `rsync`/`cp`/a working-tree checkout, which would leave `RELEASE_ID`'s `export-subst` placeholder unsubstituted and fail the deploy health gate closed); gates on `<sha>` being on `refs/remotes/origin/main`'s first-parent history — not merely reachable from it, which a merge-commit workflow makes a materially weaker claim (escape-hatched with `--allow-unmerged`, which warns loudly since this is the sole gate in front of code execution as `finance-prod`); extracts to a staging directory and verifies the archived `RELEASE_ID` before an atomic rename into `releases/<sha>/`, so a partial or content/name-mismatched copy is refused rather than installed silently — the exact failure mode named below in "Consequences"; builds the virtualenv only after that rename (a venv's console scripts carry an absolute-path shebang, so the reverse order would silently break them); removes a venv that fails post-build verification (including a real check that `--no-editable` took effect) rather than leaving one that would make `release_is_installed` report a broken release as installed; repairing the release `current` already points at moves it aside rather than deleting it outright, restoring it if the repair doesn't complete, so `current` can never resolve to nothing; idempotent and lock-serialized against concurrent runs; never deletes an old release unless `--prune` is passed. See `docs/deployment.md`'s "Release-copy step" section. |
| `finops deploy`/`rollback`/`restart` rewritten for the symlink/systemd model | **Shipped** (`src/finance_app/ops/host.py`, `src/finance_app/cli/finops.py`) — release-directory confirmation, backup gate, migration preflight, pre-promotion health check, atomic `current` repoint, `systemctl` restart (skipped with a warning until `Settings.production_units` is configured by the systemd-units PR), post-restart re-probe, auto-rollback. |
| Non-forgeable release identity for the deploy health gate | **Shipped** (`src/finance_app/ops/identity.py`) — a `RELEASE_ID` file, `git archive export-subst`-substituted, read by resolving `sys.argv[0]` through symlinks; supersedes the Docker-model's baked `IMAGE_RELEASE_ID`. See `docs/deployment.md`'s rollback-semantics section for why this is still needed under ADR-019, correcting an earlier claim in that document that it wasn't. |
| Settings/CLI explicit-prod-opt-in (dev DSN default, prod only via explicit env path) | **Shipped** (`src/finance_app/config/env.py`, `config/settings.py`) — `FINANCE_ENV_FILE` env var (no CLI flag), enforced by a `Settings` validator that refuses any DSN naming `finance_prod` unless the env file was explicitly set *and* it sets `FINANCE_ENV=production`. |
| `ops.releases.image_ref` renamed to a substrate-neutral name | **Shipped** — `artifact_ref` (migration 0006). |
| `deploy/compose.yaml`, `deploy/compose.dev.yaml`, `Dockerfile`, `src/finance_app/ops/compose.py` removed/archived | **Shipped** — `src/finance_app/ops/compose.py` was already deleted (superseded by `ops/host.py`). `deploy/compose.yaml`, `deploy/compose.dev.yaml`, and `Dockerfile` are now removed too, along with the tests that existed only to exercise them. |
| `.github/workflows/ci.yml`'s Docker-shaped jobs (`container-build`, `publish-image`, `migration-preflight`, `staging-smoke`) redesigned | **Shipped** — those four jobs are gone. Replaced by a single `release-preflight` job that mirrors `deploy/scripts/release.sh`'s own mechanism instead of an image build: `git archive HEAD` into a fresh directory (substituting `RELEASE_ID`'s `export-subst` placeholder the same way a real release would), `uv sync --no-editable` to build that release's venv, `alembic upgrade head`, then `finance selfcheck --json` — all against a throwaway `finance_ci_preflight` database in a fresh GitHub Actions Postgres service container, never `finance_dev`. `production-deploy` now depends on `release-preflight` (previously `staging-smoke`); the `packages: write`/`packages: read` permissions those four jobs needed for GHCR are gone along with them. See `docs/deployment.md`'s "CI preflight" and "Deliberately deferred" sections for the reasoning (no image/registry left for a Docker-shaped gate to attach to; `release.sh`'s own provenance check already covers "is this SHA safe to copy," so CI's remaining job covers the different question of "would this SHA work if copied"). |
| systemd units rewritten to invoke a venv binary instead of `docker compose run` | **Not shipped** — `ops/host.py:run_systemctl` and `Settings.production_units`/`systemctl_prefix` are ready to invoke them once the unit files exist. |

## Context

ADR-016 (and, underneath it, ADR-008's "immutable container releases") designed production around Docker Compose: a built image, a Compose file splitting services by credential scope, `systemd-creds`-wrapped environment variable injection into containers. That design is fully implemented and was hardened through several adversarial review rounds (see `docs/adr/ADR-016-production-deployment-control-plane.md`'s own revision history).

The owner has since decided, in two steps within one session:

1. Development and production run on **one shared VPS**, not two physically separate hosts (ADR-007/ADR-010's original assumption).
2. **No Docker at all** — not for production, not for local development. Production is a bare-metal install; the CLI and its dependencies run directly under a Python virtualenv (`uv`), invoked by systemd units, against a host-installed PostgreSQL instance.

Reasoning offered: simplicity and directness on a single-user, single-host deployment — Docker's isolation benefits (multi-tenant blast-radius containment, portable images across heterogeneous hosts) don't pay for their operational overhead when there is exactly one host, one application, and one operator. This is consistent with CLAUDE.md's standing question for any dependency: "what concrete problem does it solve *now*... and what new failure mode does it introduce?"

This ADR supersedes ADR-016 in its entirety and ADR-008 in its literal mechanism (see "Consequences" for exactly what carries over vs. what doesn't). It restates, for the new substrate, the invariants ADR-007 and ADR-010 already established for a shared VPS: Claude Code never edits production directly and never holds production credentials, regardless of what production is built from.

## Decision

### No Docker, anywhere, for this application

`deploy/compose.yaml`, `deploy/compose.dev.yaml`, and `Dockerfile` are superseded. They remain in the repository as historical artifacts until a follow-up session removes or replaces them — their continued presence is not an endorsement to extend them, and no new work should be designed around `docker compose` for this application. (CI's own use of a Postgres *service container* to provision a throwaway test database for `pytest` is GitHub Actions' own test infrastructure, unrelated to this decision and unchanged by it.)

### One PostgreSQL instance, two databases

A single, host-installed PostgreSQL server process holds two logical databases:

- **`finance_dev`** — development and local testing.
- **`finance_prod`** — production. Real financial data.

Not separate installs, not separate containers, not separate hosts — the same running `postgres` process, the same set of application roles (`finance_owner`/`finance_migrator`/`finance_app`/`finance_agent`/`finance_observer`/`finance_backup`, per `docs/database.md`) granted per-database as they already are today, just against a real host instance instead of `deploy/compose.dev.yaml`'s disposable container.

### Two environment files, two directories

- **This repository (the dev tree)** — wherever it's checked out, e.g. `~/claude-finance-app`. Claude Code operates here and only here. Configuration lives in `.env`/`.env.dev` (gitignored, already the case), pointing at `finance_dev` with synthetic/Sandbox credentials only.
- **`/opt/finance`** — the production install, structured as dated/SHA-named release directories with a `current` symlink pointing at the active one (e.g. `/opt/finance/releases/<git-sha>/`, `/opt/finance/current -> releases/<git-sha>`) — the standard bare-metal release convention, chosen so a rollback is a symlink repoint plus a restart, not a rebuild. Configuration lives in `/opt/finance/.env`, **mode 600**, owned by a dedicated production Unix user, pointing at `finance_prod` with real production credentials. Never present in this repository. Never readable by the engineering session's Unix user.

**`/opt/finance` must never be**: this directory, a second Claude Code worktree, or a Docker/Compose stack. Exactly one thing: the bare-metal production install, edited only by the release flow below, never directly.

### Claude Code must never edit `/opt/finance`

Same invariant ADR-007/ADR-010 already established for a shared VPS, restated for the bare-metal substrate: a dedicated production Unix user (not the engineering session's user) owns `/opt/finance` and its `.env`; the engineering session's user is not a member of that user's group, has no `sudo`, and cannot read either path. `.claude/settings.json`'s existing policy denials (credential-path reads, `ssh`/`scp`/`rsync`/`systemd-creds`) still apply as defense-in-depth on top of that OS-enforced boundary. See `docs/security-model.md`'s "Trust boundaries" — the mechanism it describes (directory + Unix-user + tool-policy separation on one host) does not change here, only what's *inside* the production directory does (a release tree instead of a container).

### Release flow

"Deploy" now means this, not a Docker image build/push:

1. Commit and push in the dev tree as normal — branch → PR → CI → merge, unchanged.
2. The owner (not Claude Code) copies a known, CI-green git ref into a new `/opt/finance/releases/<sha>/` directory on the VPS.
3. Back up `finance_prod` before touching it.
4. Run migrations against `finance_prod` from the new release directory (`alembic upgrade head`, using `/opt/finance/.env`'s migrator DSN).
5. Point `/opt/finance/current` at the new release directory.
6. `systemctl restart` the production units so they pick up the new code via `current`.
7. Health-check. Roll back by repointing `current` at the previous release directory and restarting if the check fails — the same "exactly one rollback step is trivial" principle ADR-008 established, now a symlink swap instead of an image tag swap.

The exact release-copy mechanism was left as a follow-up implementation decision by this ADR, beyond the ordering above — now built as `deploy/scripts/release.sh`: `git archive` from a bare mirror (not `rsync`/a working-tree checkout, which would leave `RELEASE_ID`'s `export-subst` placeholder unsubstituted), gated on ancestry of `origin/main`, with staging-then-atomic-rename extraction and a venv built only after that rename. See the implementation-status table above and `docs/deployment.md`'s "Release-copy step" section.

### systemd, not tmux/nohup

Production processes run under systemd units whose `ExecStart=` invokes the release directory's own virtualenv binary directly (e.g. `/opt/finance/current/.venv/bin/finance ...` or equivalent) — never `tmux`, `nohup`, or a manually-launched background process, from this tree or an interactive shell. This was already true under the Docker model (systemd units wrapped `docker compose run`); only what each unit's `ExecStart=` invokes changes.

### The CLI defaults to the dev DSN

`finance`/`finops` resolve their database connection from `.env`/`.env.dev` in the repository by default, pointing at `finance_dev`. They reach `finance_prod` **only** when explicitly pointed at a production environment path (e.g. an explicit `--env-file /opt/finance/.env` flag or equivalent) — never as a default, never implicitly. This is a stronger, simpler version of the same principle ADR-010 already required (production credentials never reachable by accident from an engineering session): the default must be incapable of touching production data at all, not merely configured not to.

## Consequences

- **ADR-016 (Production deployment control plane) is superseded in its entirety.** Its subject — Compose service topology, per-job credential environment variables via `with-production-env.sh`, `src/finance_app/ops/compose.py` — no longer applies to where this project is going. Kept as a historical record of the abandoned approach (and of the several rounds of adversarial hardening that went into it, which is not wasted work — several of its underlying principles, like "one rollback step is trivial" and "narrow credential scope per job," carry forward). Do not implement any further part of it.
- **ADR-008 (Immutable container releases and rollback) is superseded in its literal mechanism** — there is no container image, no registry, no image tag. Its *principle* (exactly one rollback step guaranteed trivial; current/previous releases always tracked; post-deploy health checks trigger automatic rollback) carries over unchanged to the symlink-based release model above.
- **ADR-011 (systemd timers for scheduled single-host jobs) is not superseded.** systemd timers/units were always the scheduling mechanism regardless of Docker; only each unit's `ExecStart=` changes, from `docker compose run ...` to a direct venv-binary invocation.
- **ADR-015 (Backup encryption, GPG symmetric) is not superseded in principle** (GPG-symmetric encryption of backup archives before they leave the host is unaffected by the packaging mechanism), but its implementation assumed a `finance_backup` Compose service; a follow-up session adapts it to run `pg_dump` directly against the host `finance_prod` database from a systemd-invoked script.
- **ADR-007 (Git/CI-CD only path to production) and ADR-010 (Production secrets excluded from the engineering session) are unchanged in principle** and only need their concrete paths updated (`/opt/finance` instead of `/opt/finance-app`; no Docker image in the release-integrity story, a copied release directory instead) — both already anticipated a shared-VPS, non-network boundary; this ADR just changes what's *inside* the boundary.
- **Milestone 7's Docker-shaped CI jobs need redesigning**, not merely updating: `container-build`/`publish-image`/`migration-preflight`/`staging-smoke` all assume an image. A follow-up session replaces them with whatever "prove this release installs and passes health checks" looks like for a bare-metal release directory (plausibly: a fresh venv build + smoke test against a scratch `finance_dev`-shaped database in CI, still using a GitHub Actions Postgres *service container* for that scratch database — CI's own test infra is unrelated to this ADR).
- **One less moving part**: no image registry, no image pull step, no container runtime to keep patched on the production host. A new failure mode in exchange: the release-copy step must be as carefully gotten right as `docker pull` used to be — a partial or corrupted copy is now this project's own problem to guard against rather than the container runtime's. `deploy/scripts/release.sh` addresses this directly (staging-directory extraction with an atomic rename, and a `RELEASE_ID`-equality check before that rename ever happens) rather than leaving it as an open risk.

## Revisit when

As the follow-up implementation session builds this out, update the "Implementation status" table above to move items from "Not shipped" to "Shipped" — matching the pattern ADR-016 already established. Once every row is shipped, remove the table and this note.

One prior CI guarantee was dropped in the same Docker-removal change without a written replacement: the old `migration-preflight` job proved migrations apply cleanly on top of the *previously published release's* schema (QA-17 — "a migration that only works from nothing can still break a real upgrade-in-place"), while `release-preflight` only migrates a fresh empty database. **Required before the first production deploy**: restore that upgrade-in-place coverage (see `docs/deployment.md`'s "Deliberately deferred").
