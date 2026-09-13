#!/usr/bin/env bash
# PreToolUse guard (ADR-018 §4). Mechanically enforces a rule that was
# prose-only since Milestone 0: CLAUDE.md's working agreement is "branch per
# unit of work; PR into main. No direct commits to main" -- yet `git commit`
# and `git push` both sit in settings.json's unconditional allow list.
# Nothing previously stopped a session from committing or pushing straight
# to main other than remembering not to.
#
# Blocks:
#   1. `git commit` while the current branch is `main`.
#   2. `git push` whose target resolves to `main` (explicit `origin main`,
#      `HEAD:main`, `refs/heads/main`, or a bare push while checked out on
#      `main` with an upstream already tracking it).
#
# Does NOT block read-only or branch-management git commands (checkout,
# switch, fetch, pull, merge, worktree, etc.) -- only the two actions that
# actually write history to `main`. The merge-to-main path stays
# `.claude/scripts/merge-class-a.sh` (a server-side gh pr merge, not a local
# push), which this hook does not touch.
#
# Exit 2 = block the tool call and return stderr to Claude.
set -uo pipefail

payload=$(cat)

extract() {
  # $1: python expression fragment evaluated against the parsed payload `d`.
  printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print('"$1"')
' 2>/dev/null
}

tool_name=$(extract 'd.get("tool_name", "")')
command=$(extract '(d.get("tool_input", {}) or {}).get("command", "").replace(chr(10), " ")')
cwd=$(extract 'd.get("cwd", "")')

[[ "$tool_name" == "Bash" ]] || exit 0
[[ -n "$command" ]] || exit 0

repo_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
work_dir="${cwd:-$repo_root}"

current_branch=$(git -C "$work_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

is_git_commit() {
  # Matches `git commit ...` as a whole command or after a `&&`/`;`/`|` chain,
  # without false-positiving on `git commit-graph`, `git log --grep=commit`, etc.
  printf '%s' "$command" | grep -qE '(^|[;&|]\s*)git[[:space:]]+commit([[:space:]]|$)'
}

is_git_push() {
  printf '%s' "$command" | grep -qE '(^|[;&|]\s*)git[[:space:]]+push([[:space:]]|$)'
}

push_targets_main() {
  printf '%s' "$command" | grep -qE '(^|[;&|]\s*)git[[:space:]]+push\b.*\b(origin[[:space:]]+main\b|main:main\b|HEAD:main\b|HEAD:refs/heads/main\b|refs/heads/main\b)'
}

block() {
  echo "BLOCKED: $1" >&2
  echo "CLAUDE.md's working agreement: branch per unit of work, PR into main. No direct commits to main." >&2
  echo "Class A changes merge via .claude/scripts/merge-class-a.sh (a gh pr merge, not a local push). Class B/C wait for the owner." >&2
  exit 2
}

if is_git_commit && [[ "$current_branch" == "main" ]]; then
  block "attempted 'git commit' while checked out on main."
fi

if is_git_push; then
  if push_targets_main; then
    block "attempted 'git push' with main as the explicit target."
  fi
  if [[ "$current_branch" == "main" ]]; then
    # A bare `git push` (or `git push origin`/`git push -u origin main` via
    # tracking config) while on main resolves to main by definition even
    # without main appearing literally in the command text.
    block "attempted 'git push' while checked out on main."
  fi
fi

exit 0
