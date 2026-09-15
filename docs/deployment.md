# Deployment

Milestone 7 deliverable (handoff §29). Covers the production topology, the release/rollback sequence, and how CI/CD, `finops`, and systemd fit together. **Rewritten 2026-09-13 for ADR-019** (bare-metal, no Docker, `/opt/finance`) — supersedes the Docker Compose design ADR-016 described; see that ADR's own superseded note and ADR-019 for the full decision and its implementation-status table. See `docs/architecture.md` for the system-wide picture, `docs/security-model.md` for the invariants this design enforces, `docs/backups.md` for backup/restore, and `docs/runbooks/deploy.md` for the exact owner-performed procedure.

## What exists today vs. what this document describes

As of 2026-09-13, `finops deploy`/`rollback`/`restart` are rewritten for ADR-019's bare-metal symlink model — `src/finance_app/ops/host.py` (release-directory/`systemctl` invocation, superseding `src/finance_app/ops/compose.py`, now deleted), `src/finance_app/ops/identity.py` (non-forgeable release identity via `git archive export-subst`), and `config/settings.py`'s explicit-prod-opt-in guard (`FINANCE_ENV_FILE`) are all shipped. **Now also shipped**: `deploy/scripts/release.sh`, the release-copy step `finops deploy` refuses to perform itself — see the "Release-copy step" section below. **Still not done**: the systemd unit files (`Settings.production_units` defaults empty — `deploy`/`restart` skip the actual restart and say so until those land), and CI's Docker-shaped jobs (`container-build`/`publish-image`/`migration-preflight`/`staging-smoke`) haven't been redesigned yet — a follow-up session's work. `Dockerfile`, `deploy/compose*.yaml`, and every other file under `deploy/scripts/` besides `release.sh` are deliberately left in place, now orphaned, to be removed together with their own test coverage in that follow-up PR — do not treat them as current guidance. **No production deployment is provisioned yet**: `/opt/finance` doesn't exist on the VPS, and neither does the second `finance_prod` database — see `docs/runbooks/deploy.md`.

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

## `finops deploy` / `rollback` / `restart`

These three commands stay the *only* place `finops` changes production state (every other command — `health`, `sync-status`, `db-status`, `migration-status`, `backup-status`, `recent-errors` — stays a read-only query through `finance_observer`). All three accept `--release-root` (default `Settings.release_root`, `/opt/finance`) — this is also what makes the whole path unit-testable against a `tmp_path` with no `/opt/finance` and no root anywhere in the process.

- **`finops deploy <sha>`** — validates `<sha>` looks like a Git SHA; refuses (never copies) unless `<release_root>/releases/<sha>/` is already installed — the release-copy mechanism is `deploy/scripts/release.sh`, a separate step (see "Release-copy step" below); refuses unless a recent successful backup is on record (ADR-019's release step 3, "back up `finance_prod` before touching it" — taking the backup stays the owner's/timer's job, this only gates on one existing); runs the migration preflight from the new release's own tree (`<release>/.venv/bin/alembic upgrade head`); health-checks the new release **by path, before touching `current` at all** (`ops.status.deploy_health_check`) — this is possible under the bare-metal model in a way `docker pull` never allowed, since probing a release directory doesn't require it to be `current`; on success, atomically repoints `current`, restarts configured systemd units (skipped with a warning if `Settings.production_units` is empty), and re-probes *through* `current` before committing bookkeeping. Unhealthy at any point → automatic rollback to the previous release directory (ADR-008's principle, unchanged).
- **`finops rollback`** — probes the previous release by path, atomically repoints `current` to it, restarts, re-probes through `current`, then commits the bookkeeping flip — in that order, so a crash between the filesystem change and the bookkeeping write leaves bookkeeping stale but never wrong in a way that blocks a retry (re-running `rollback` finds the symlink already correct and just finishes the write). No rebuild, nothing to pull — even more literally "no registry fetch beyond what's already local" than the Docker design, since there's no registry at all.
- **`finops restart`** — a liveness re-check of the currently-recorded release *through* the `current` symlink, unchanged in spirit from ADR-016 D2 (there is still no long-running application process until Milestone 8's webhook). Also refuses if `ops.releases` bookkeeping disagrees with what `current` actually resolves to on disk (`ops.status.release_topology`) — a disagreement a mutable symlink can produce that a container tag never could. Never changes `current` or bookkeeping.

None of them accept a SQL string, a shell string, or an SSH target — the command surface stays fixed (handoff §10's "no arbitrary SQL/shell" applies to deployment exactly as it applies to diagnosis). What they shell out to is `src/finance_app/ops/host.py` (a release directory's own `.venv/bin/*` and `systemctl`, superseding `docker compose`/`src/finance_app/ops/compose.py`, now deleted) — the bookkeeping module (`src/finance_app/ops/release.py`) needed no conceptual change, since it never depended on Docker in the first place (it only tracks release identifiers and current/previous/failed/rolled_back status; only its `image_ref` column was renamed to `artifact_ref`, migration 0006).

## Release-copy step

`deploy/scripts/release.sh <sha>` is what puts a real, installed release directory in place before `finops deploy` will touch it — the two halves meet exactly at `ops/host.py:release_is_installed`'s definition (a real directory containing a built `.venv/bin/finance`) and nowhere else; `release.sh` never touches the database, `current`, or `systemctl`.

It archives from a bare mirror at `/opt/finance/repo.git` (`docs/runbooks/deploy.md` §1) via `git archive`, never `rsync`/`cp`/a working-tree checkout — the repository root's `RELEASE_ID` file (`$Format:%H$`, marked `export-subst` in `.gitattributes`) only gets substituted with the real commit SHA by `git archive`, and that substitution is the non-forgeable release identity `ops/identity.py` depends on for the deploy health gate. Guarantees, in order:

- refuses a `<sha>` that is not on `refs/remotes/origin/main`'s **first-parent** history on the mirror, unless `--allow-unmerged` is passed (which prints a loud warning — bypassing this is the sole gate in front of code execution as `finance-prod`). Deliberately membership in the first-parent chain, not mere `git merge-base --is-ancestor` reachability: this repository merges PRs with merge commits, so a multi-commit PR's individual intermediate commits stay reachable from `main` forever after merge without any one of them ever having been a PR head or a `main` tip that CI's `push: branches: [main]` job itself evaluated — `--is-ancestor` would accept any of those silently, while first-parent membership contains exactly the commits that job ran on. This is the closest available proxy for "this went through CI and review" now that ADR-019 removed the image-publish gate — see "Deliberately deferred" below for the open question this doesn't fully answer;
- extracts into a staging directory and only `mv`s it into `releases/<sha>/` after verifying the archived `RELEASE_ID` equals `<sha>` — the direct guard against "a partial or corrupted copy is now this project's own problem" (ADR-019's own "Consequences"), and specifically against `git archive <other-sha> | tar -x -C releases/<sha>` succeeding silently;
- builds the virtualenv *after* that rename, never before — `uv sync` bakes absolute paths into every console script's shebang, so building in a staging directory and renaming afterwards would silently produce a `.venv/bin/finance` with a shebang pointing nowhere;
- verifies the result by actually running the release's own interpreter (`--no-editable`'s effect is checked by asking the built interpreter where it imports `finance_app` from, not just trusted from the flag) — and removes a `.venv` that fails any of these checks, so a rejected build can never look installed to `release_is_installed`;
- is idempotent (re-running against an already-installed, identity-matching sha is a no-op; an incomplete one is rebuilt from scratch) and lock-serialized against itself (`releases/.lock`, breaking only a lock whose pid is provably dead);
- never deletes anything unless `--prune` is passed, and even then never removes whatever `current` points at or the release just installed — see `docs/runbooks/deploy.md` §3 for the residual risk this heuristic (rather than reading `ops.releases`) carries.

## Health checks and automatic rollback

Unchanged in design from before — `finops health` (read-only, `finance_observer`) aggregates database reachability, migration-head match, sync freshness, and backup verification freshness exactly as it did under the Docker design; `finance-health.timer` still runs the same aggregate check every 15 minutes via the `finance_app` role. None of this was ever Docker-specific.

## Rollback semantics

ADR-008's principle, carried into ADR-019 exactly as stated there: exactly one rollback step is guaranteed trivial — current ↔ previous, now a `current` symlink repoint instead of an image tag swap. `ops.releases` (or its bare-metal equivalent) tracks at most one `current` row and at most one `previous` row; anything older is left as plain history. If a second consecutive deploy also fails, `finops rollback` still returns to the last-known-good release, because a failed deploy attempt never touches the `current`/`previous` bookkeeping.

**Correction to an earlier draft of this document**: an earlier version of this section claimed the `wrong_image`/build-time-identity verification ADR-016 D3 added (QA-37) "has no equivalent need under ADR-019: there is no image to mistake for another." That is wrong, and the implementation does not follow it. Two concrete failure modes survive the move off Docker:

1. **`current` can point at the wrong release.** The symlink repoint is a step anyone with filesystem access to `/opt/finance` can perform by hand (`docs/runbooks/deploy.md`'s own rollback procedure is `ln -sfn`), and a crash between a repoint and its bookkeeping commit leaves exactly this disagreement.
2. **A release directory's contents can disagree with its name.** ADR-019 names this itself (see that ADR's "Consequences"): "a partial or corrupted copy is now this project's own problem to guard against rather than the container runtime's." `git archive <other-sha> | tar -x -C releases/<sha>` succeeds silently.

So `ops/status.py:probe_release` still compares an identity that must originate *inside* the release tree, never in the environment the caller supplies — the same non-forgeability requirement QA-37 established, just with a different source: a plain `RELEASE_ID` file at the repository root, tracked with the literal content `$Format:%H$` and `.gitattributes` marking it `export-subst`. `git archive <sha>` (the release-copy mechanism this project's own runbook already names) substitutes the real commit SHA into that file *at archive time*, before `finops deploy` ever runs — a plain checkout (this dev tree, a stray `git worktree add`) never substitutes it, so it can never impersonate a real release. `ops/identity.py:installed_release_id()` reads it back by resolving `sys.argv[0]` through symlinks — deliberately not from `finance_app.__file__` (which resolves into `.venv/lib/python3.*/site-packages/` for a non-editable install, nowhere near the release root) — which is also what lets a post-restart probe check `current` *through the symlink* and catch failure mode 1 above, not just a freshly-deployed release the Docker model never needed to re-check.

## Deliberately deferred

- **`/opt/finance` provisioning, systemd unit files, and CI's Docker-shaped jobs redesigned.** See ADR-019's implementation-status table for the exact remaining rows; `finops deploy`/`rollback`/`restart`, Settings' explicit-prod-opt-in guard, and `deploy/scripts/release.sh` are shipped (above).
- **A CI gate on "is this SHA safe to copy to `/opt/finance`"** — `release.sh`'s first-parent-of-`origin/main` check keeps a copy from happening at all for a ref that never merged, but that only re-derives what CI itself already decided; it is not a CI-side gate in its own right (there is no artifact for one to attach to, now that there's no image/registry). Whether one belongs at all, and what it would check beyond what the ancestry gate already does, is still open.
- **Real production provisioning on the shared VPS** — the engineering workspace's own host is the intended production host (ADR-007/ADR-010/ADR-019), but `/opt/finance`, its Unix user, and `finance_prod` don't exist yet. `docs/runbooks/deploy.md` documents the procedure.
- **Milestone 8's webhook endpoint** — was scaffolded via a Compose `caddy` service under the old design; a follow-up session needs a bare-metal equivalent (a host-installed reverse proxy, or Python serving TLS directly) once this milestone's rewrite lands.
- **A "canary"/gradual rollout mechanism** — out of scope for a single-user, single-instance application; all-or-nothing health-gated promotion is the appropriate amount of ceremony here (CLAUDE.md: don't add complexity without a demonstrated need).
