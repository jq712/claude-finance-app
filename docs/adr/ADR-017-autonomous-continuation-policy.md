# ADR-017: Autonomous continuation policy — Class A auto-merge, Class B/C hold for a human

**Status:** Accepted

Refines handoff §25 (Incident Classes and Autonomous Authority) and §32 (Behavior Expected From
the Lead Claude Code Session). Does not change the Class A/B/C definitions themselves — only
what autonomous authority each class carries when a session is asked to keep working without a
human checking in on every step.

## Context

The owner wants to be able to start a Claude Code session on this project — interactively, by
resuming one, or via a scheduled cloud routine (`RemoteTrigger`) — and have it keep working
through the milestone backlog (handoff §29, continuation prompt §34) without needing to be
present for every decision. That has been the project's stated intent since §32 ("work
autonomously... ask only when truly blocked"), but two gaps made it unsafe to actually rely on
unattended, for a project whose whole reason for existing is handling real financial data:

1. **Class A's authority was written as "merge, and deploy"** (handoff §25, mirrored in
   `CLAUDE.md` and `docs/security-model.md`). Read literally, an autonomous session hitting a
   lint fix or a patch dependency bump was authorized to push it straight to production. That
   was never actually reachable — Milestone 7's real release sequence
   (`docs/deployment.md`) gates `production-deploy` behind the `production` GitHub
   Environment's manual approval and an explicit `[owner, on the VPS] finops deploy <sha>`
   step, for every class, and no production deployment is provisioned yet regardless (as of
   2026-09-13 the "VPS" itself is shared with the engineering workspace, bare-metal, no Docker —
   ADR-007/ADR-010/ADR-019 — but the production side of that boundary still doesn't exist). But
   the *wording*
   promised more autonomy than the built system grants, which is exactly the kind of gap that
   bites the first time it's exercised rather than read.
2. **§32's "ask only when truly blocked" assumes someone is there to answer.** It's fine advice
   for an interactive session. It gives no instruction for what an unattended session should do
   when it actually hits one of those blockers — wait forever, guess, or retry in a loop are
   all bad outcomes, and nothing ruled any of them out.

Separately: this repository's GitHub plan does not expose the branch-protection API
(`GET /branches/main/protection` returns 403 — "Upgrade to GitHub Pro or make this repository
public"). There is no platform-level control available to mechanically restrict which PRs can
be auto-merged. The boundary this ADR sets has to be enforced by the agent reading and following
this document and CLAUDE.md — the same way every other invariant in this project that lacks a
`.claude/settings.json` deny-rule or a hook is enforced today (handoff §13: "Back the NEVER list
with mechanical enforcement wherever possible" — wherever it isn't possible, the prose is what
there is).

## Decision

**This is the standing default whenever a session is asked to continue the backlog
autonomously — not a special mode that has to be invoked separately, and not tied to any one
trigger mechanism.** An interactive session working through §34's continuation prompt, a
session resumed later, and a scheduled `RemoteTrigger` cloud routine all operate under the same
contract:

- **Class A** — diagnose, patch, get required CI green (never bypassed, never weakened to pass),
  and **merge to `main` autonomously**. Before merging, confirm via `gh pr checks` (or
  equivalent) that every required check is green — never merge on red, pending, or a missing
  check.
- **Class B** — implement fully, including the independent review this class already requires
  (`security-reviewer` and `qa-adversarial` as separate invocations from whoever implemented
  it, per CLAUDE.md's delegation rule), open the PR — **then stop.** Do not merge it. It waits
  for the owner.
- **Class C** — stop destructive automation, preserve evidence, write the incident report, do
  not touch `main`. Unchanged from handoff §25 — this ADR grants no new authority here.
- **Merge is never deploy, for any class.** Production deploy is always an owner-performed
  `finops deploy` on the VPS (`docs/deployment.md`'s release sequence). This holds
  unconditionally until Milestone 9 exists to define autonomous deployment's own rollback
  criteria — autonomous continuation's scope is implementation work (Milestones 7 and 8 today),
  never production operations.
- **No one present to answer** (handoff §32): on any of §32's enumerated blockers, or a Class C
  condition, stop the current unit of work cleanly and leave a durable note — a PR description,
  a commit message, or a GitHub issue, whichever fits what was in progress — rather than idle
  waiting for a response that isn't coming, and rather than guessing. Never retry a blocked
  action in a loop. Finish all other independent, unblocked work first, same as the interactive
  case.

## Consequences

- The owner can start or schedule a session and walk away without it silently exceeding the
  authority they intended: at most, low-risk changes land on `main` unattended; anything
  touching migrations, Plaid sync semantics, financial math, credentials, webhook security, or
  agent permissions always waits for them, as a reviewed PR, not a merged one.
- Morning-after (or any-time-after) review has a fixed shape: check what merged (Class A),
  review and merge or reject what's waiting (Class B), and read any incident notes (Class C /
  blockers). `docs/runbooks/autonomous-continuation.md` documents that check.
- The Class-A-only merge boundary is enforced by this document and CLAUDE.md, not by GitHub
  branch protection — this repo's plan doesn't expose that API. If the plan is ever upgraded or
  the repo made public, adding real branch protection (required reviews for anything not from
  a recognized Class-A-only automation path) would be strictly additive to this ADR, not a
  replacement for it.
- A session can no longer read Class A as "and deploy." Any future work that *does* want
  autonomous production deployment has to be its own explicit decision (Milestone 9), not an
  incidental reading of this one.

## Revisit when

- Milestone 9 (Autonomous maintenance) is built and defines automatic-deployment rollback
  criteria — at that point this ADR should be revisited to decide whether Class A deploy
  authority is introduced deliberately, superseding the "merge is never deploy" clause above.
- The GitHub plan changes such that branch-protection rules become available — worth adding a
  mechanical Class-A-only merge restriction at that point rather than relying on prose alone.
- The owner decides they want Class B to auto-merge after independent review too (a materially
  different autonomy decision from what's recorded here — would need its own explicit sign-off,
  not an incremental drift of this ADR).
