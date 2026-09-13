# Runbook: autonomous continuation sessions

Operational companion to [ADR-017](../adr/ADR-017-autonomous-continuation-policy.md) and
[ADR-018](../adr/ADR-018-autonomous-engineering-workflow-v2.md). Those set the policy and the
mechanism (Class A auto-merges via a mechanical gate, Class B/C wait for the owner); this
runbook is what *you*, the owner, actually look at afterward — whether the session was started
interactively, resumed, or by a scheduled routine.

## What a session does under this policy

This procedure now lives in one place — the `autonomous-continuation` Skill
(`.claude/skills/autonomous-continuation/SKILL.md`) — not here. Read it there; this runbook
intentionally does not restate it, so the contract only has to change in one place (ADR-018 §2).
In one line: inventory the repo, classify the next unit of work, implement it, review it
(fresh-context, every class), and end the run as a Class A merge, an open Class B PR, or a
durable note — never an idle wait.

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
- The routine's prompt should point at the `autonomous-continuation` Skill — it should not
  restate the autonomy contract independently, so the contract only has to be changed in one
  place if it's ever revised.
