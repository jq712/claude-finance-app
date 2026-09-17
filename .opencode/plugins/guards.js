// OpenCode guards plugin — the OpenCode equivalent of Claude Code's
// .claude/settings.json hook registrations. Deliberately a thin shim: it
// synthesizes the Claude-shaped stdin payload the existing
// .claude/hooks/*.sh scripts already parse, and executes those scripts
// unchanged, so the guard logic has exactly one source of truth that both
// harnesses run.
//
// Exit code 2 from a script = block the tool call (thrown as an Error, which
// is OpenCode's only documented way to abort a call from
// tool.execute.before).
//
// Note: OpenCode's tool.execute.before input has no per-call `cwd`; the shim
// substitutes this plugin's load-time directory/worktree, which is correct
// for normal single-repo sessions.

import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"

const EDIT_TOOLS = ["edit", "write", "apply_patch"]

// Port of .claude/settings.json's permissions.deny Read(...) globs — OpenCode's
// permission.read does not accept per-path globs, so these live here.
const SECRET_READ_PATTERNS = [
  /\.(pem|key|p12|credential)$/i,
  /(^|\/)credentials\.json$/,
  /(^|\/)\.env$/,
  /(^|\/)\.env\.(local|development|staging|production)$/,
  /(^|\/)secrets\//,
  /^\/etc\/finance(-app)?(\/|$)/,
  /^\/etc\/credstore/,
  /(^|\/)\.plaid\//,
]

// The one sanctioned unattended commit path (.opencode/scripts/commit.sh).
// It is a script, not a raw `git commit`, so it never matches the commit
// regex below; this recognition states the sanctioned boundary in one place.
// It is deliberately NOT a skip for raw `git commit` -- that stays guarded --
// and it matches only a bare, un-chained invocation, so a command that
// appends its own `git commit` still falls through to the guard.
const SANCTIONED_COMMIT =
  /^\s*(?:\.\/)?\.opencode\/scripts\/commit\.sh\s+(?:"[^"]*"|'[^']*'|[^\s;&|`$()<>\\]+)\s*$/

function isSanctionedCommit(command) {
  return SANCTIONED_COMMIT.test(command)
}

function gitCurrentBranch(repoRoot) {
  const result = spawnSync("git", ["-C", repoRoot, "rev-parse", "--abbrev-ref", "HEAD"], {
    encoding: "utf8",
  })
  if (result.status !== 0) return ""
  return (result.stdout || "").trim()
}

// Inline stand-in for .claude/hooks/guard-protected-branch.sh, used only while
// that hook script is absent from this checkout (it exists only on PR #15's
// branch). Blocks the two actions that write history to main — `git commit`
// while on main, and any `git push` whose target resolves to main — and fails
// closed (blocks) when the current branch cannot be determined. Everything
// else passes.
function enforceProtectedBranchShim(repoRoot, command) {
  // The sanctioned wrapper enforces its own branch checks (it refuses main and
  // detached HEAD) and accepts exactly one message argument. Recognized only
  // as a bare, un-chained invocation; anything else falls through so a raw
  // `git commit` sharing the command line is still evaluated below.
  if (isSanctionedCommit(command)) return { status: 0, stdout: "", stderr: "" }

  const isCommit = /(^|[;&|]\s*)git\s+commit(\s|$)/.test(command)
  const isPush = /(^|[;&|]\s*)git\s+push(\s|$)/.test(command)
  if (!isCommit && !isPush) return { status: 0, stdout: "", stderr: "" }

  const branch = gitCurrentBranch(repoRoot)
  const block = (reason) => ({
    status: 2,
    stdout: "",
    stderr:
      `BLOCKED: ${reason}\n` +
      "CLAUDE.md's working agreement: branch per unit of work, PR into main. No direct commits to main.\n" +
      "Class A changes merge via .opencode/scripts/merge-class-a.sh (a gh pr merge, not a local push). Class B/C wait for the owner.",
  })

  if (isCommit) {
    if (branch === "main") return block("attempted 'git commit' while checked out on main.")
    if (branch === "") {
      return block(
        "attempted 'git commit' but the current branch could not be determined " +
          "(guard-protected-branch.sh is missing) — failing closed rather than risk a commit to main.",
      )
    }
    return { status: 0, stdout: "", stderr: "" }
  }

  const pushTargetsMain =
    /(^|[;&|]\s*)git\s+push\b.*\b(origin\s+main\b|main:main\b|HEAD:main\b|HEAD:refs\/heads\/main\b|refs\/heads\/main\b)/.test(command)
  if (pushTargetsMain) return block("attempted 'git push' with main as the explicit target.")
  if (branch === "main") return block("attempted 'git push' while checked out on main.")
  if (branch === "") {
    return block(
      "attempted 'git push' but the current branch could not be determined " +
        "(guard-protected-branch.sh is missing) — failing closed rather than risk a push to main.",
    )
  }
  return { status: 0, stdout: "", stderr: "" }
}

function runHook(repoRoot, script, payload, { ifMissing = "block" } = {}) {
  const scriptPath = path.join(repoRoot, ".claude", "hooks", script)
  if (!existsSync(scriptPath)) {
    if (typeof ifMissing === "function") {
      return ifMissing()
    }
    throw new Error(
      `BLOCKED: guard script '.claude/hooks/${script}' is missing — refusing to run unguarded rather than failing open.`,
    )
  }
  return spawnSync("bash", [scriptPath], {
    input: payload,
    encoding: "utf8",
    env: { ...process.env, CLAUDE_PROJECT_DIR: repoRoot },
    timeout: 120000,
  })
}

function blockOrPass(result, what) {
  if (result.status === 2) {
    const message = (result.stderr || result.stdout || "").trim()
    throw new Error(message || `BLOCKED: ${what} refused by project guard.`)
  }
  if (result.error) throw result.error
}

function appendOutput(output, text) {
  if (typeof output.output === "string") output.output += text
  else if (typeof output.title === "string") output.title += text
}

export async function Guards({ directory, worktree }) {
  const repoRoot = worktree || directory

  return {
    "tool.execute.before": async (input, output) => {
      const tool = input.tool
      const args = (output.args || input.args) || {}

      if (tool === "read") {
        const filePath = args.filePath || ""
        if (filePath) {
          const denied = SECRET_READ_PATTERNS.find((re) => re.test(filePath))
          if (denied) {
            throw new Error(
              `BLOCKED: reading '${filePath}' — secret material. AGENTS.md forbids the agent reading this path.`,
            )
          }
        }
        return
      }

      if (EDIT_TOOLS.includes(tool)) {
        const filePath = args.filePath || ""
        const payload = JSON.stringify({
          tool_name: "Edit",
          tool_input: { file_path: filePath },
          cwd: repoRoot,
        })
        blockOrPass(
          runHook(repoRoot, "guard-applied-migrations.sh", payload),
          "file edit",
        )
        return
      }

      if (tool === "bash") {
        const command = args.command || ""
        const payload = JSON.stringify({
          tool_name: "Bash",
          tool_input: { command },
          cwd: repoRoot,
        })
        blockOrPass(
          runHook(repoRoot, "guard-protected-branch.sh", payload, {
            ifMissing: () => enforceProtectedBranchShim(repoRoot, command),
          }),
          "shell command",
        )
      }
    },

    "tool.execute.after": async (input, output) => {
      if (!EDIT_TOOLS.includes(input.tool)) return
      const args = input.args || {}
      const filePath = args.filePath || args.file_path || ""
      if (!filePath || !filePath.endsWith(".py")) return

      const payload = JSON.stringify({
        tool_name: "Edit",
        tool_input: { file_path: filePath },
      })
      const result = runHook(repoRoot, "python-checks.sh", payload)

      if (result.status === 2) {
        throw new Error((result.stderr || result.stdout || "ruff check failed").trim())
      }
      // Exit 0 with stderr = python-checks.sh's "this is NOT a pass" warning.
      // tool.execute.after has no stderr channel to the model, so fold it into
      // the tool output — otherwise the anti-false-confidence signal is lost.
      const stderr = (result.stderr || "").trim()
      if (stderr) appendOutput(output, `\n${stderr}`)
    },
  }
}
