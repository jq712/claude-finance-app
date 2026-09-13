#!/usr/bin/env bash
# PostToolUse: format and lint the Python file that was just edited.
# Exit 2 returns the lint output to Claude so it fixes the issue immediately.
#
# ADR-018 §4: `command -v uv` alone is not a reliable "uv doesn't exist"
# check -- verified in the reference sandbox, `uv` resolves fine through an
# interactive shell's PATH (~/.local/bin) but NOT through the minimal PATH a
# hook subprocess actually runs with. The old version of this hook treated
# "not on my PATH" as "not installed" and silently exited 0 either way --
# indistinguishable from "ran ruff, it's clean." That's worse than no hook:
# it manufactures false confidence. This version tries known install
# locations before giving up, and when it truly can't find uv, it says so
# loudly instead of staying silent. CI (.github/workflows/ci.yml) is the
# actual gate regardless -- this hook is fast local feedback, not the
# enforcement mechanism, per the repo's own operating experience that local
# sandboxes vary and tests/lint can't be assumed to run outside CI.
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

uv_bin=""
for candidate in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    uv_bin="$candidate"
    break
  fi
done

if [[ -z "$uv_bin" ]]; then
  echo "NOTE: local ruff check skipped -- 'uv' not found on PATH or in \$HOME/.local/bin, \$HOME/.cargo/bin." >&2
  echo "This is NOT a pass: $file_path has not been format/lint-checked locally. CI runs ruff on every PR and is the actual gate -- do not report this file's lint state as clean until CI (or a manual uv run) has actually checked it." >&2
  exit 0
fi

"$uv_bin" run --project "$repo_root" ruff --version >/dev/null 2>&1 || {
  echo "NOTE: local ruff check skipped -- '$uv_bin run ruff --version' failed (project not synced?). CI is the actual gate; do not report this file's lint state as clean." >&2
  exit 0
}

"$uv_bin" run --project "$repo_root" ruff format -q "$file_path" >/dev/null 2>&1

if ! lint_output=$("$uv_bin" run --project "$repo_root" ruff check --fix "$file_path" 2>&1); then
  echo "ruff check failed on $file_path:" >&2
  echo "$lint_output" >&2
  exit 2
fi

exit 0
