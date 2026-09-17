#!/usr/bin/env bash
# The ONLY sanctioned path for an unattended OpenCode session to create a
# commit. Raw `git commit` stays in opencode.json's "ask" bucket and is still
# blocked on `main` by .opencode/plugins/guards.js -- this wrapper does not
# replace that guard, it is the narrow, auditable door a session may use
# without a human at the prompt.
#
# It refuses to be clever: exactly one argument, a non-empty message that does
# not begin with `-` (so it can never be read as a flag), and a checked-out
# branch that is neither `main` nor detached HEAD. It then runs exactly
# `git commit -m "$1"` -- no --amend, no -n/--no-verify, no extra flags, no eval.
#
# Usage: commit.sh "message"
set -euo pipefail

usage() {
  echo "usage: $(basename "$0") \"message\"" >&2
  exit 64
}

[[ $# -eq 1 ]] || usage
[[ -n "$1" ]] || { echo "REFUSED: commit message is empty." >&2; exit 1; }
[[ "$1" != -* ]] || { echo "REFUSED: commit message must not start with '-'." >&2; exit 1; }

branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" != "main" ]] || { echo "REFUSED: refusing to commit while on main." >&2; exit 1; }
[[ "$branch" != "HEAD" ]] || { echo "REFUSED: refusing to commit on a detached HEAD." >&2; exit 1; }

git commit -m "$1"
