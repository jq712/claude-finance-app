# Runbook: autonomous continuation sessions

Operational companion to [ADR-017](../adr/ADR-017-autonomous-continuation-policy.md). That ADR
sets the policy (Class A auto-merges, Class B/C wait for the owner); this runbook is what to
actually look at, whether the session was started interactively, resumed, or by a scheduled
`RemoteTrigger` routine.

## What a session does under this policy

Given the handoff §34 continuation prompt (or a scheduled routine configured with it), a
session:

1. Inventories the repo against `CLAUDE_FINANCE_APP_HANDOFF.md` from the first incomplete
   milestone step.
2. Implements the next coherent unit of work on a branch.
3. For a Class A change: gets required CI green, confirms via `gh pr checks` (never on
   red/pending/missing), and merges to `main` itself.
4. For a Class B change: implements it fully, including independent `security-reviewer` /
   `qa-adversarial` review (separate invocations, per CLAUDE.md's delegation rule), opens the
   PR — and stops. It does not merge.
5. For a Class C condition or any handoff §32 blocker with nobody present to answer: stops
   cleanly and leaves a note (PR description, commit message, or GitHub issue) rather than
   idling or guessing.
6. Never touches production. `finops deploy`/`rollback` stay an owner-performed action
   regardless of class (ADR-017) — no production deployment is provisioned yet, and even once
   it is, the autonomous session's Unix user has no read access to `/opt/finance` or its
   credentials (ADR-007/ADR-010/ADR-019, revised 2026-09-13 — engineering and production share
   a VPS, no Docker; see `docs/security-model.md`'s "Trust boundaries").

## What to check after a run

- **Merged commits on `main` since you last looked** (`git log main --oneline` from the repo,
  or the PR list filtered to merged) — these are Class A changes the session already shipped.
  Skim them; nothing here should need action, but this is where a misclassification would show
  up.
- **Open PRs** (`gh pr list --state open`) — these are Class B changes, implemented and
  reviewed, waiting on you to actually merge. Review and merge (or request changes) same as any
  other PR.
- **Anything left as a note** — a PR description flagging a blocker, an unusually-worded commit
  message, or a new GitHub issue — this is where a Class C condition or a §32 blocker (missing
  credential, an unresolvable product decision, DNS/domain choice, etc.) surfaces. Read these
  before assuming the backlog just wasn't touched.
- If nothing merged and no PR opened and no note exists, the session either found nothing
  unblocked to do or didn't run — check `RemoteTrigger`'s `list_runs`/`get_run_log` if it was a
  scheduled routine (see below), or the session transcript if it was interactive.

## If a scheduled `RemoteTrigger` routine is set up

Not required by ADR-017 — the policy applies equally to a manually-started session. If one
exists:

- View/manage it at `https://claude.ai/code/routines/{id}`, or via the `RemoteTrigger` tool:
  `{action: "list"}`, `{action: "get", trigger_id: ...}`.
- Inspect a specific firing with `{action: "list_runs", trigger_id: ...}` then
  `{action: "get_run_log", session_id: ...}`.
- Pause it by disabling the routine (`{action: "update", trigger_id: ..., body: {"enabled":
  false}}`) rather than deleting it — routines can't be deleted via the API; use
  `https://claude.ai/code/routines` for that.
- The routine's prompt should point at the handoff's §34 continuation prompt and this ADR —
  it should not restate the autonomy contract independently, so the contract only has to be
  changed in one place if it's ever revised.
