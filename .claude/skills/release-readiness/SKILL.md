---
description: Deploy/release-readiness checklist for Milestone 7-9 deploy-topology work (docs/deployment.md's release sequence, rollback bookkeeping, backup verification) plus the currently-known unresolved findings to check against rather than silently reintroduce. Use when reviewing a deploy-topology PR, or before an owner-performed finops deploy.
---

# Release readiness

Consolidates the `sre-release` agent's release sequence and `docs/deployment.md`. Used two ways:
reviewing a PR that touches `deploy/`, `src/finance_app/ops/`, or `finops` (always Class B —
credentials, deploy topology, per CLAUDE.md's Risk classes); and as the owner's own pre-flight
check before running `finops deploy` for real.

## Release sequence (ADR-007, ADR-008, docs/deployment.md)

Clean expected revision → required CI green → image built and tagged by immutable Git SHA
(never `latest` as the sole identifier) → migration preflight against the *previously published*
release's schema, not just an empty database → staging-smoke deploy → smoke tests →
critical financial evals → sanitized health/log inspection → production deploy (owner-performed
`finops deploy <sha>`, never automated, regardless of risk class — ADR-017) → post-deploy health
verification → release recorded in `ops.releases` → automatic rollback if health checks cross
defined failure criteria.

Always know the current *and* previous known-good release so rollback is one command
(`finops rollback`) — ADR-008 guarantees exactly one rollback step is trivial
(current ↔ previous), not an arbitrary-depth undo stack.

## Health check components (`finops health`)

`database` reachable · `migrations` applied revision matches repo head · `sync` most recent
`ops.sync_runs` row isn't `error` and isn't stale (~36h) · `backup` most recent backup has a
successful restore-verification within 14 days. `overall` is healthy only if database/migrations
are fine and sync isn't `error`/`stale`; a fresh install with no sync yet is `never_run`, not
unhealthy.

## Known unresolved findings — check these are actually fixed, don't just trust the PR title

These came out of a real round-3 review of this project's deploy topology (PR #13). If a new PR
touches release/rollback/health-check logic, verify each of these explicitly rather than
assuming a prior fix covered it — a title claiming a deliverable ships is not the same as it
actually shipping (this happened once already: a PR title claimed a D8 deliverable — writing
`/etc/finance-app/env` via `finops current-release` — that no code in the diff actually
implemented):

- **Release identity is provably checked, not just echoed back to itself.** A health check that
  compares a runtime-injected `RELEASE_ID` env var against the same var read back proves
  nothing — it can never detect a wrong image running. Verify the check ties to something the
  image actually carries.
- **No resolve-then-act race.** If rollback resolves "the target release" once, probes it, then a
  separate call re-resolves independently, a concurrent deploy/rollback between those two steps
  can promote a release that was never actually verified. Verify one resolution is reused
  end-to-end, not re-derived.
- **Interrupted-deploy `pending` rows are handled**, not just left for `get_previous` (or
  equivalent) to skip past silently.
- **Health-check phases have blanket exception handling.** A `PermissionError` or a single
  non-UTF-8 byte on a subprocess's stdout must not crash with a raw traceback and leave a release
  stuck `pending` with no rollback attempted.
- **Structured-output parsing (e.g. a self-check sentinel) can't be overridden by an
  earlier/unrelated log line that happens to match the same sentinel shape** — verify it can't
  report a broken release as healthy via a log-injection-shaped false positive.

## Before signing off

- Backup restore-verified within 14 days — not assumed, checked (`finops backup-status`).
- Rollback actually exercised in staging for this change if it touches rollback logic at all —
  "the code looks right" is not the same as "the rollback ran and worked."
- Run `pre-merge-review` in addition to the required `qa-adversarial` + `security-reviewer` gate
  this class of change already requires (CLAUDE.md's Risk classes, ADR-017).
- `.claude/scripts/merge-class-a.sh` refuses anything touching `deploy/` by path regardless of
  how it's classified (ADR-018 §6) — this class of change always waits for the owner's own
  `gh pr merge`, which is the correct outcome for Class B work either way.
