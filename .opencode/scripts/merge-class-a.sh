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
# believing the change is Class B. See .opencode/skills/autonomous-continuation
# and AGENTS.md's Risk classes.
#
# Usage: merge-class-a.sh <pr-number>
#
# Exit-code contract (AGENTS.md §13.5; pinned in .orchestrator/pins). The
# caller interprets the result by exit code only; stderr text is diagnostic.
#   0   merged -- `gh pr merge` succeeded. Nothing that happens after the
#       merge may change this.
#   2   policy refusal -- the script deliberately declined (reserved path,
#       wrong class, unsafe diff, any check the script itself defines).
#       Deterministic; re-running gives 2 again. No merge was attempted.
#   3   technical failure -- a command the script depends on failed or
#       produced unreadable output (network, auth, gh error, git error,
#       missing file). The PR may or may not have been merged: query it,
#       never re-run this script.
#   64  usage error -- bad arguments. No merge was attempted.
# No other exit code is produced by this script; unhandled command failures
# are mapped to 3 by the ERR trap below.
set -euo pipefail
trap 'exit 3' ERR

usage() {
  echo "usage: $(basename "$0") <pr-number>" >&2
  exit 64
}

[[ $# -eq 1 ]] || usage
[[ "$1" =~ ^[0-9]+$ ]] || usage
pr="$1"

# Policy refusal (exit 2): the script itself declined; no merge attempted.
refuse() {
  echo "REFUSED: $*" >&2
  exit 2
}

# Technical failure (exit 3): a dependency failed or its output was unreadable.
fail() {
  echo "FAILED: $*" >&2
  exit 3
}

command -v gh >/dev/null 2>&1 || fail "gh CLI not found"

# --- 1. No conflicts, nothing stale ----------------------------------------
mergeable=$(gh pr view "$pr" --json mergeable --jq '.mergeable' 2>/dev/null) ||
  fail "could not read PR #$pr (does it exist?)"
[[ "$mergeable" == "MERGEABLE" ]] ||
  refuse "PR #$pr mergeable state is '$mergeable', not MERGEABLE."

# --- 2. Every reported check is green, and enough checks were reported -----
# SKIPPED is accepted: this repo's own CI intentionally skips its CD-stage
# jobs (publish-image, migration-preflight, staging-smoke, production-deploy)
# on pull_request events -- they only run on push to main -- so requiring
# SUCCESS on those would make no PR ever mergeable. The count floor guards
# the "checks haven't populated yet" race: ADR-017 says never merge on a
# missing check, and an empty or too-small list looks exactly like one.
states=$(gh pr checks "$pr" --json state --jq '.[].state' 2>/dev/null) ||
  fail "could not read checks for PR #$pr"
check_count=$(printf '%s\n' "$states" | grep -c . || true)
(( check_count >= 5 )) ||
  refuse "only $check_count checks reported for PR #$pr -- looks incomplete, not green."
if printf '%s\n' "$states" | grep -qvE '^(SUCCESS|SKIPPED)$'; then
  echo "REFUSED: PR #$pr has a check that is not SUCCESS/SKIPPED:" >&2
  gh pr checks "$pr" >&2 || true
  exit 2
fi

# --- 3. Path screen: never auto-merge anything inherently non-Class-A ------
# Migrations, deploy topology, the autonomy tooling itself, ADRs, the Plaid
# and agent boundaries, and the documents that define these rules in the
# first place all require a human's own gh pr merge click, however trivial
# any individual line looks. This is what makes this very change permanently
# ineligible for the mechanism it introduces.
sensitive_pattern='^(migrations/versions/|deploy/|\.claude/|docs/adr/|src/finance_app/plaid/|src/finance_app/agent/)|^(CLAUDE\.md|AGENTS\.md|CLAUDE_FINANCE_APP_HANDOFF\.md|docs/security-model\.md)$'
files=$(gh pr view "$pr" --json files --jq '.files[].path' 2>/dev/null) ||
  fail "could not read changed files for PR #$pr"
if printf '%s\n' "$files" | grep -qE "$sensitive_pattern"; then
  echo "REFUSED: PR #$pr touches a path reserved for human-reviewed merge:" >&2
  printf '%s\n' "$files" | grep -E "$sensitive_pattern" >&2
  echo "This is Class B (or workflow-tooling) by path, whatever class it was implemented under. Leave it open for the owner." >&2
  exit 2
fi

echo "PR #$pr: mergeable, $check_count checks reported and green, no reserved paths touched."
gh pr merge "$pr" --squash ||
  fail "gh pr merge exited nonzero for PR #$pr -- merge outcome unknown, do not re-run"
# The merge landed. No cleanup happens here (AGENTS.md §13.7 owns it), and
# nothing after this point may change the exit status.
exit 0
