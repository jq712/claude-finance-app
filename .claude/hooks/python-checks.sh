#!/usr/bin/env bash
# PostToolUse: format and lint the Python file that was just edited.
# Silent no-op until the Python project actually exists (Milestone 0).
# Exit 2 returns the lint output to Claude so it fixes the issue immediately.
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

[[ "$file_path" != *.py ]] && exit 0
[[ -f "$file_path" ]] || exit 0

repo_root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
[[ -f "$repo_root/pyproject.toml" ]] || exit 0
command -v uv >/dev/null 2>&1 || exit 0
uv run --project "$repo_root" ruff --version >/dev/null 2>&1 || exit 0

uv run --project "$repo_root" ruff format -q "$file_path" >/dev/null 2>&1

if ! lint_output=$(uv run --project "$repo_root" ruff check --fix "$file_path" 2>&1); then
  echo "ruff check failed on $file_path:" >&2
  echo "$lint_output" >&2
  exit 2
fi

exit 0
