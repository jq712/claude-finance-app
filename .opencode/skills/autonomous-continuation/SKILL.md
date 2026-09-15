---
name: autonomous-continuation
description: Continue the finance-app milestone backlog autonomously (handoff §29/§32/§34, ADR-017) — boots the session against repo state, classifies the next unit of work as Class A/B/C, executes it, and ends every run with a merge, an open PR, or a durable note. Use whenever asked to "continue the backlog," "keep working autonomously," "pick up where the last session left off," or when resumed/scheduled with no further instruction.
---

# Autonomous continuation

This is the single source of truth for what an unattended-or-unattended-capable session does on
this repo. `docs/runbooks/autonomous-continuation.md` is the owner-facing "what to check after a
run" companion — this Skill is the session-facing procedure. If you change the contract, change
it here; do not let the runbook or a scheduled-routine prompt restate it independently (that's
how it drifts).

The authority contract itself (what Class A/B/C each get to do) is
[ADR-017](../../../docs/adr/ADR-017-autonomous-continuation-policy.md). Read it before assuming
you remember it correctly, especially if this is a fresh session.

## 1. Boot

Re-derive state from git/GitHub — never trust memory of "where things were":

```
git status
git branch --show-current
git log main --oneline -20
gh pr list --state open
```

Cross-reference `README.md`'s Status line against `CLAUDE_FINANCE_APP_HANDOFF.md` §29 to find
the first incomplete milestone step. If `README.md` and the actual repo state disagree (a
milestone the README calls complete but the code doesn't support, or vice versa), trust the
code and treat the README line as itself a small Class A fix.

**If resuming a worktree** (`.claude/worktrees/<slug>/`): before treating any uncommitted
changes there as new work, run `git diff --stat HEAD`. If that diff looks like it exactly
reverts an already-merged fix (matches a recent commit's diff in reverse), it is almost
certainly stale/corrupted state from an interrupted prior session, not real new work — quarantine
it with a uniquely-tagged `git stash push -u -m "..."` (capture the SHA immediately) rather than
building on it. This already happened once in this repo; it will happen again if this check is
skipped.

## 2. Delegate research, don't inline it

For anything requiring reading more than one or two files to understand (an unfamiliar module,
"how does X currently work," verifying an OpenCode/Plaid/GitHub Actions API detail per handoff
§32), delegate to a subagent (the `task` tool) or fork rather than reading it all into this
session's own context. Keep this session's context for decisions and integration, not raw
research material.

## 3. The plan agent vs. direct execution

Multi-file or architecturally uncertain work: switch to the `plan` agent first. A single
well-scoped fix (a clear regression, a lint issue, a narrowly-specified feature already fully
decided by the handoff): implement directly. Don't switch to the `plan` agent for a one-line
fix; don't skip planning a multi-module change because a session wants to move fast.

## 4. Classify before implementing

Per AGENTS.md's Risk classes (handoff §25), decide Class A / B / C **before** writing code, and
write that classification into what will become the PR description. Getting it wrong in the
cautious direction costs time; getting it wrong the other way risks exactly what ADR-017 exists
to prevent. When genuinely unsure, classify up.

## 5. Implement

- **Class A**: implement, test, done — no independent-review gate beyond `pre-merge-review`
  (step 6), which applies here too.
- **Class B**: implement fully, then launch `security-reviewer` and `qa-adversarial` as
  **parallel** `task` calls in one message (separate fresh-context invocations — never the
  session that wrote the change reviewing it). If schema changed, also invoke `database`. Fold
  every finding back in before opening the PR.
- **Class C**: stop. Do not implement a fix. Follow `docs/incident-response.md`'s Class C
  procedure — preserve evidence, do not touch `main`, write the incident report, escalate. This
  is itself a correct, complete unit of "work" for this session.

## 6. Fresh-context correctness review — mandatory, every class

Before opening or marking any PR ready: invoke `pre-merge-review` (see that Skill). This applies
to Class A work too — previously nothing gave Class A an independent read before CI. Correctness
findings block; everything else is optional.

## 7. Open the PR

Description must include: risk class, what changed and why, exact test commands run, and a
rollback note. Push with `git push` (an ask-listed command; pushing to `main` itself is
mechanically blocked by the guards plugin regardless of intent).

## 8. Merge or stop, per class

- **Class A**: confirm every required check is actually green —
  `.opencode/scripts/merge-class-a.sh <PR>` does this itself and refuses to merge otherwise (see
  the script's own comments for exactly what it checks). This is the *only* sanctioned merge path from an
  autonomous session. Never call bare `gh pr merge` expecting it to go through unattended — it
  sits in `opencode.json`'s `permission.bash` ask list on purpose and will simply stall with
  nobody there to approve it.
- **Class B**: open the PR, then **stop**. Do not merge it, do not poll waiting for the owner to
  merge it. It waits.
- **Class C** or any handoff §32 blocker with nobody present to answer: stop the current unit of
  work cleanly. Leave a durable note — a PR description, a commit message, or
  `gh issue create` — explaining exactly what's blocked and the recommended
  default. Never idle-wait for a response that isn't coming. Never retry a blocked action in a
  loop hoping it resolves itself. Finish all other independent, unblocked work first.

## 9. End the session

Every run ends as exactly one of:

- a merged Class A commit (via the script in step 8),
- an open Class B PR awaiting the owner, or
- a durable note/issue explaining a blocker.

Never an idle wait with nothing recorded. If a milestone actually completed in this run, update
`README.md`'s Status line in the same change — `docs-freshness` in CI checks this mechanically
for any PR whose title or branch name references a milestone, but check it yourself regardless;
it has gone stale before.

## Headless / scheduled entry points

`opencode run "<prompt>"` (local, non-interactive) just needs to invoke this Skill — point the
prompt at it (a natural-language equivalent that matches this file's `description`), never
restate the contract inline in the scheduling config itself. One source of truth.

Cloud scheduled routines (Claude Code's `RemoteTrigger`) have no OpenCode equivalent. If
unattended continuation is needed on this machine, the available substitute is a
`systemd --user` timer invoking `opencode run`; its unit file must point at this Skill, not
restate the contract.
