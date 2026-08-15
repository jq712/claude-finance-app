#!/usr/bin/env bash
# PreToolUse guard. Blocks edits that CLAUDE.md forbids, mechanically.
#
# 1. An Alembic migration that is already committed to git is "applied history".
#    Rewriting it desynchronizes deployed databases from the migration chain.
#    New (untracked) migration files are fine.
# 2. Real secret files are never edited by an agent.
#
# Exit 2 = block the tool call and return stderr to Claude.
set -uo pipefail

payload=$(cat)

file_path=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get("tool_input", {}).get("file_path", ""))
' 2>/dev/null)

[[ -z "$file_path" ]] && exit 0

repo_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
rel_path="${file_path#"$repo_root"/}"

case "$rel_path" in
  .env|.env.[!e]*|secrets/*|*.pem|*.key)
    echo "BLOCKED: '$rel_path' holds secret material. CLAUDE.md forbids agent edits here." >&2
    echo "Use .env.example for placeholders. Production secrets live only in the production runtime boundary." >&2
    exit 2
    ;;
esac

case "$rel_path" in
  migrations/versions/*.py)
    if git -C "$repo_root" ls-files --error-unmatch "$rel_path" >/dev/null 2>&1; then
      echo "BLOCKED: '$rel_path' is a committed migration — treat it as applied history." >&2
      echo "Create a NEW migration that makes the corrective change forward:" >&2
      echo "  uv run alembic revision -m '<what this corrects>'" >&2
      echo "See CLAUDE.md (NEVER: rewrite an already-applied migration) and handoff §4.9." >&2
      exit 2
    fi
    ;;
esac

exit 0
