#!/usr/bin/env bash
# The ONLY sanctioned path from an autonomous OpenCode session to an actual
# merge into `main` (ADR-017). Bare `gh pr merge` stays in opencode.json's
# "ask" bucket on purpose -- this script exists precisely because "ask"
# stalls forever with nobody present to answer it, so the thing that
# replaces it re-verifies mechanically instead of trusting whatever the
# calling session believes about its own change.
#
# This is a safety NET, not a substitute for classifying the change
# correctly at authoring time. A session should never reach for this script
# believing the change is Class B. See docs/adr/ADR-017-autonomous-continuation-policy.md
# and AGENTS.md §10 (class recomputed from the diff), §13.5 and §13.6.
#
# Usage: merge-class-a.sh <pr-number>
#
# Exit-code contract (AGENTS.md §13.5; pinned in .orchestrator/pins). The
# caller interprets the result by exit code only; stderr text is diagnostic.
#   0   merged -- `gh pr merge` succeeded. Nothing that happens after the
#       merge may change this.
#   2   policy refusal -- the script deliberately declined. Exactly two
#       conditions reach it: a changed path matched the reserved-path
#       pattern, or mergeable is CONFLICTING. Both depend only on the
#       PR's changed files and the branch/base relationship, so neither
#       is transient: they persist until the branch or base changes.
#       The path screen runs FIRST, ahead of both transient gates, so a
#       reserved-path PR returns 2 whatever its mergeable state and
#       however few of its checks have reported; CONFLICTING is reached
#       only once the path screen has passed. No merge was attempted.
#   3   technical failure -- a command the script depends on failed or
#       produced unreadable output (gh missing, gh error, network,
#       auth), or the PR's state is not yet determinable (mergeable not
#       yet computed, too few checks reported, a check still pending),
#       or a reported check is not SUCCESS/SKIPPED, which is a red PR
#       rather than a not-yet one. Only the `gh pr merge` failure path
#       leaves the merge outcome in doubt; every other path exits before
#       any merge is attempted. Query the PR, never re-run this script.
#   64  usage error -- bad arguments. No merge was attempted.
# No other exit code is produced by this script; unhandled command failures
# are mapped to 3 by the ERR trap below.
set -eEuo pipefail
trap 'exit 3' ERR

usage() {
  echo "usage: $(basename "$0") <pr-number>" >&2 || true
  exit 64
}

[[ $# -eq 1 ]] || usage
[[ "$1" =~ ^[0-9]+$ ]] || usage
pr="$1"

# Policy refusal (exit 2): the script itself declined; no merge attempted.
refuse() {
  echo "REFUSED: $*" >&2 || true
  exit 2
}

# Technical failure (exit 3): a dependency failed or its output was unreadable.
fail() {
  echo "FAILED: $*" >&2 || true
  exit 3
}

command -v gh >/dev/null 2>&1 || fail "gh CLI not found"

# --- 1. Path screen: never auto-merge anything inherently non-Class-A ------
# This runs FIRST, ahead of the mergeable and check gates, because a
# reserved path is a permanent property of the PR's diff while those two
# gates are transient. Run later, the screen let a mid-flight check turn
# a standing refusal (2) into a not-yet (3) and hid the fact that no
# amount of waiting would make the PR auto-mergeable. It needs only
# `gh pr view --json files` and reads nothing the later gates compute,
# so a reserved-path PR now exits 2 whatever its mergeable state and
# however few of its checks have reported.
#
# Migrations, deploy topology, `.claude/`, ADRs, the Plaid and agent
# boundaries, the enforcement boundary itself -- `.opencode/` (this
# script, plugins/guards.js, agents/), `opencode.json` (the ask/allow
# buckets that keep a bare `gh pr merge` behind a human) and
# `.orchestrator.example/` (the pins template, which carries this file's
# own hash) -- and the documents that define these rules in the first
# place all require a human's own gh pr merge click, however trivial any
# individual line looks.
#
# So a change touching these files is Class B by path and this script
# will not merge it: the enforcement boundary cannot auto-merge a change
# to itself or to its own configuration. The set is now closed over the
# pins: every file §5 pins by hash -- this script, plugins/guards.js,
# opencode.json, AGENTS.md -- is also reserved here, so the two
# mechanisms cover the same files from two directions. The pin notices
# one of them moving; this screen keeps it from moving through an
# automatic merge in the first place. Widening the list stays an
# owner-supervised change -- Appendix A.6 puts "any change to
# merge-class-a.sh reserved paths" out of scope for an ordinary PR.
#
# The test below is a here-string on purpose. Written as
# `printf '%s\n' "$files" | grep -qE ...`, `grep -q` exits at the first
# match, the writer takes SIGPIPE, and under `set -o pipefail` the
# pipeline reports 141 -- so a matched reserved path in a large diff read
# as NO match and the PR merged. Measured: it flipped somewhere above a
# few thousand changed paths. Never reintroduce a pipe into this test.
# The diagnostic `grep -E` below it keeps its pipe safely: without `-q`
# it reads to EOF, so the writer is never signalled, and its status is
# discarded anyway.
sensitive_pattern='^(migrations/versions/|deploy/|\.claude/|docs/adr/|src/finance_app/plaid/|src/finance_app/agent/|\.opencode/|\.orchestrator\.example/)|^(CLAUDE\.md|AGENTS\.md|CLAUDE_FINANCE_APP_HANDOFF\.md|docs/security-model\.md|opencode\.json)$'
files=$(gh pr view "$pr" --json files --jq '.files[].path' 2>/dev/null) ||
  fail "could not read changed files for PR #$pr"
if grep -qE "$sensitive_pattern" <<<"$files"; then
  echo "REFUSED: PR #$pr touches a path reserved for human-reviewed merge:" >&2 || true
  printf '%s\n' "$files" | grep -E "$sensitive_pattern" >&2 || true
  echo "This is Class B (or workflow-tooling) by path, whatever class it was implemented under. Leave it open for the owner." >&2 || true
  exit 2
fi

# --- 2. No conflicts, nothing stale ----------------------------------------
mergeable=$(gh pr view "$pr" --json mergeable --jq '.mergeable' 2>/dev/null) ||
  fail "could not read PR #$pr (does it exist?)"
case "$mergeable" in
  MERGEABLE)   ;;
  CONFLICTING) refuse "PR #$pr mergeable state is 'CONFLICTING', not MERGEABLE." ;;
  *)           fail "PR #$pr mergeable state is '$mergeable', not yet computed or unreadable" ;;
esac

# --- 3. Every reported check is green, and enough checks were reported -----
# SKIPPED is accepted because ci.yml's ten jobs do not all run on every PR:
# production-deploy is gated on push to main, and docs-freshness only runs
# for a PR whose base is main. On a pull_request that does not match, each
# of those reports SKIPPED, so demanding SUCCESS from every job would make
# no PR ever mergeable. The other eight -- lint-and-typecheck, unit-tests,
# integration-tests, security-tests, agent-evals, secret-scan,
# release-preflight, dependency-scan -- run unconditionally.
# The floor of 5 is a deliberate bare literal and this script never reads
# it from anywhere; the pinned script hash is what enforces it, and
# .orchestrator/pins carries min_checks_reported as documentation only.
# It guards the "checks haven't populated yet" race: ADR-017 says never
# merge on a missing check, and an empty or too-small list looks exactly
# like one. That race is transient, so both gates below exit 3, never 2:
# a state the script cannot yet evaluate is a technical failure, not a
# deliberate refusal (AGENTS.md §14.2).
#
# The red-check test is a here-string for the same reason the path screen
# is: as `printf ... | grep -qvE ...`, `grep -qv` exits at the first
# non-green state, the writer takes SIGPIPE, `set -o pipefail` reports
# 141, the `if` goes false and the bailout below is SKIPPED -- a red PR
# merges. That fails open, so it is worse than the path screen's version
# of the same bug. Measured: caught at 5002 reported states, missed at
# 20002 and above. The two pipes around it are safe and stay: `grep -c`
# and the `grep -vE | sort -u` both read to EOF, so neither ever signals
# the writer, and both discard their status anyway.
states=$(gh pr checks "$pr" --json state --jq '.[].state' 2>/dev/null) ||
  fail "could not read checks for PR #$pr"
check_count=$(printf '%s\n' "$states" | grep -c . || true)
(( check_count >= 5 )) ||
  fail "only $check_count checks reported for PR #$pr -- not yet complete, not a refusal"
if grep -qvE '^(SUCCESS|SKIPPED)$' <<<"$states"; then
  observed=$(printf '%s\n' "$states" | grep -vE '^(SUCCESS|SKIPPED)$' | sort -u | tr '\n' ' ' || true)
  echo "PR #$pr has a check that is not SUCCESS/SKIPPED:" >&2 || true
  gh pr checks "$pr" >&2 || true
  fail "PR #$pr check state(s) ${observed% } -- not yet complete or not green, not a refusal"
fi

echo "PR #$pr: mergeable, $check_count checks reported and green, no reserved paths touched." ||
  fail "could not write the status line for PR #$pr -- refusing to merge blind"
gh pr merge "$pr" --squash ||
  fail "gh pr merge exited nonzero for PR #$pr -- merge outcome unknown, do not re-run"
# The merge landed. No cleanup happens here (AGENTS.md §13.7 owns it), and
# nothing after this point may change the exit status.
exit 0
