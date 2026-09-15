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

// Hook scripts that have been skipped this session, so a missing hook logs
// once rather than on every tool call.
const skippedHooks = new Set()

function runHook(repoRoot, script, payload, { ifMissing = "block" } = {}) {
  const scriptPath = path.join(repoRoot, ".claude", "hooks", script)
  if (!existsSync(scriptPath)) {
    if (ifMissing === "skip") {
      if (!skippedHooks.has(script)) {
        skippedHooks.add(script)
        console.error(
          `[guards] WARNING: skipping guard '.claude/hooks/${script}' — hook script is not present in this checkout (it exists only on PR #15's branch). ` +
            `Running without its protection. All other guards (secret-file reads, tracked-migration edits, python-checks) remain active.`,
        )
      }
      return { status: 0, stdout: "", stderr: "" }
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
          runHook(repoRoot, "guard-protected-branch.sh", payload, { ifMissing: "skip" }),
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
