---
description: Mandatory fresh-context correctness review before any unit of work is called done or a PR is opened/marked ready — Class A included. Wraps the native /code-review Skill with this project's policy on what blocks vs what's optional. Use before declaring any change finished.
---

# Pre-merge review

Before this ADR-018, only Class B work got an independent read of its own diff
(`security-reviewer` + `qa-adversarial`, separate invocations). Class A — most of what an
autonomous-continuation session actually produces — got none: the same context that wrote the
diff was the only context that ever looked at it before CI. This Skill closes that gap for
**every** unit of work, not just Class B.

## What to do

Invoke the native `/code-review` Skill against the diff for this unit of work (the branch's
changes, or the specific files touched), at `high` effort. Don't reach for `ultra` here by
default — it's a multi-agent cloud review with real cost, appropriate as an owner-discretionary
*supplement* on top of Class B's already-mandatory specialist review, not as this routine gate.

**If the session invoking this Skill is the same context that authored the diff, dispatch the
review as a separate `Agent` invocation** rather than reading the diff again in the same
context. A second pass in the same context that just finished writing the code is not fresh
eyes — it already knows why every line is there, which is exactly the blind spot this Skill
exists to route around. `/code-review` invoked directly may already do this via its own
subagent dispatch; when authoring and reviewing would otherwise happen in the same context,
don't rely on that — spawn the review explicitly.

## Triage

- **Correctness findings (bugs, logic errors, a failure scenario with concrete inputs) block.**
  Fix them, or explain in the PR description exactly why a specific finding doesn't apply, before
  proceeding.
- **Everything else — simplification, reuse, efficiency, style — is explicitly optional and
  non-blocking.** Note in the PR description if something was deliberately skipped; don't let
  a style disagreement stall a change that's otherwise correct and tested.

## When this is required

Every time, no exceptions, before a PR is opened or marked ready — including Class A. For Class
B work, this runs *in addition to* the mandatory `security-reviewer`/`qa-adversarial` gate
(CLAUDE.md's Risk classes), not instead of it: this Skill's scope is general correctness: their
scope is this project's specific invariants (secrets, tool boundaries, sync semantics, money
arithmetic), which a generic diff reviewer isn't positioned to catch.
