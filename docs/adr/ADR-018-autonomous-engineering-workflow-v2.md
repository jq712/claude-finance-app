# ADR-018: Autonomous engineering workflow v2 — roster, Skills, mechanical merge gate, session lifecycle

**Status:** Accepted

Supersedes handoff §12 (Autonomous Claude Code Engineering Organization), §14 (Claude Code
Skills / Reusable Workflows), and §15 (MCP, Hooks, and External Tools Policy). Refines — does
**not** alter the authority of — [ADR-017](ADR-017-autonomous-continuation-policy.md)'s Class
A/B/C contract: Class A still auto-merges on green CI, Class B still implements-and-waits,
Class C still stops and reports. This ADR is about the *mechanism* that contract runs on, not
the contract itself.

## Context

Handoff §12–15 were written at Milestone 0, before any code existed, and they said so
explicitly: one orchestrator plus ephemeral specialists, no Skills until a workflow is proven
manually, avoid MCP, no second orchestration layer. That was the right default for a project
with no operating history. Six milestones later, the workflow has operating history, and it
has surfaced concrete, specific gaps that the pre-code design couldn't have anticipated:

1. **Agent prompts have drifted from decisions made after they were written.**
   `.claude/agents/architecture.md` still says "the boundary... between the OpenAI-backed
   runtime agent and the Claude Code engineering control plane" — but
   [ADR-014](ADR-014-provider-interchangeable-runtime-agent.md) made the runtime agent
   provider-interchangeable (OpenAI *or* Anthropic, via `AGENT_PROVIDER`) a full milestone ago.
   Nothing forced that update because nothing ever re-reads a subagent's prompt against the
   ADRs it was supposed to track.
2. **`gh pr merge` sitting in `settings.json`'s `ask` bucket is correct with a human present
   and silently wrong without one.** ADR-017 promises Class A changes "merge to `main`
   autonomously." A scheduled routine or an unattended interactive session hits `ask`, gets no
   answer, and the merge simply never happens — indistinguishable from the session having found
   nothing to do. `ask` is a safety property only when someone is there to answer it.
3. **The `PostToolUse` lint hook's `uv` detection is a silent no-op by construction.**
   `command -v uv >/dev/null 2>&1 || exit 0` is reasonable-looking and actually dangerous:
   verified in this sandbox, an interactive shell resolves `uv` via `~/.local/bin` on `PATH`,
   but a bare minimal-`PATH` subprocess — exactly the shape a hook runs with — does not. The
   hook then exits 0, which is bit-for-bit identical to "ran ruff, it's clean." A hook whose
   failure mode is silence provides negative value: it's worse than having no hook, because it
   manufactures false confidence.
4. **Nothing requires a fresh-context, correctness-focused read of a diff before calling work
   done.** Class B gets independent review by design (`security-reviewer` + `qa-adversarial`,
   separate invocations). Class A — the majority of autonomous-continuation traffic — gets none:
   the same context that wrote the diff is the only context that ever looks at it before CI.
5. **Ad hoc parallel work already exists and already caused an incident.**
   `.claude/worktrees/adr016-fixes` predates any written convention for worktree use. It already
   produced a real failure mode (a worktree whose uncommitted state was a silent full revert of
   an already-merged fix, discovered only by diffing against `HEAD` — see the `repo-quirks`
   operating memory). An undocumented pattern that already bit once will bite again.
6. **The roster and the classification contract have no mechanical backing beyond prose and
   `settings.json`'s deny list.** This repository's GitHub plan doesn't expose branch
   protection (confirmed 403, per ADR-017). Every boundary this project actually cares about —
   Class-A-only autonomous merge, no direct commits to `main`, writer ≠ reviewer — is enforced
   today by an agent reading and following documents. CLAUDE.md already states the principle
   ("back the NEVER list with mechanical enforcement wherever possible... prefer a hook over an
   instruction whenever the rule must hold even if the model forgets it") but hasn't applied it
   to the merge boundary or the protected-branch rule.

**Explicit authorization:** the owner waived handoff §12/§14/§15's specific prescriptions for
this change (2026-09-12) and asked for a redesign built on the current Claude Code toolkit —
subagents, Skills, hooks, settings permissions, worktrees, headless/scheduled entry points —
while explicitly preserving: every secret-handling deny rule, the applied-migration edit guard,
the runtime financial agent's product boundaries (untouched by this ADR), ADR-017's Class A/B/C
authority contract, writer-≠-reviewer separation with review roles carrying no write tools, and
`claude-code/`'s frozen-snapshot status.

## Decision

### 1. Agent roster — refreshed, not reinvented

The six specialists already matched handoff §12.2–12.7 structurally. The problem was content
drift and undocumented tool-grant choices, not the wrong set of roles. Kept, with changes:

| Agent | Change | Why |
|---|---|---|
| `architecture` | Corrected "OpenAI-backed runtime agent" → provider-interchangeable language; added a pointer to `.claude/skills/pre-merge-review` and this ADR | Was stale against ADR-014; an architecture reviewer citing the wrong provider model undermines its own authority |
| `database` | Added `WebSearch`; added pointer to `.claude/skills/safe-migration` | Postgres/Alembic behavior questions need current docs, not just fetchable URLs already in hand; the migration checklist is now a Skill, not just prose buried in this agent's prompt |
| `implementation` | Added a line requiring the `pre-merge-review` Skill before calling anything done | Closes gap 4 for the class of work (Class A) that previously got zero independent read |
| `qa-adversarial` | Expanded "Infrastructure attacks" into a concrete release/rollback/health-check section built from real findings (PR #13 round-3: `probe_release`'s tautological image-identity check, the rollback race between resolve-then-probe and a second independent resolve, the unhandled `pending`-row case, missing blanket exception handling around health checks, log-injection into `_parse_selfcheck_stdout`'s sentinel parsing); added a webhook attack subsection for Milestone 8; added `WebFetch` | The old list ("OpenAI timeout, 429... duplicate systemd execution") predates Milestone 7's actual attack surface. Real findings from a real review are a better checklist than a generic one, and this agent needs to verify real Plaid/webhook behavior when modeling attacks |
| `security-reviewer` | Added a release-identity line to the checklist (does the running container's identity provably trace to the image that was reviewed, or is it a value the process echoes back to itself) | Direct response to the `wrong_image` tautology found in review — a security reviewer should have been positioned to catch that class of gap by prompt, not by luck |
| `sre-release` | Added `WebSearch`; added pointer to `.claude/skills/release-readiness` | Same current-docs need as `database`; the release checklist is now a Skill so it's consulted, not just embedded in one agent's prompt |

No write-tool grant changed on any review-oriented agent — `security-reviewer` still has zero
write tools; that boundary is a survivor, not a redesign target.

**Two additions considered and rejected:**

- **A dedicated Milestone-8 webhook specialist.** Rejected: webhook work decomposes cleanly into
  existing boundaries — endpoint code is `implementation`'s job, signature/replay/exposure
  review is already `security-reviewer`'s job (its checklist already has a Webhook section),
  and replay/race attacks are already `qa-adversarial`'s job (extended above). A new agent per
  milestone deliverable fragments review responsibility instead of deepening it.
- **A bespoke `code-reviewer` subagent for fresh-context diff review.** Rejected: Claude Code
  already ships `/code-review` as a native Skill with its own effort levels and an `ultra`
  multi-agent cloud mode. Building a parallel implementation to get "someone reads the diff
  fresh" duplicates a capability that already exists and now has to be maintained twice. See §3.

**Review flow** (Class B; Class A gets the same shape minus the security/adversarial step,
added instead as `pre-merge-review`):

```
implementation writes the change
        │
        ├──► database          (parallel, if schema touched)
        │
        ├──► qa-adversarial    (parallel, fresh context — writes regression tests, does not fix)
        │
        └──► security-reviewer (parallel, fresh context, no write tools — reports, does not fix)
                        │
                        ▼
              pre-merge-review Skill  (fresh-context /code-review, correctness-blocking)
                        │
                        ▼
              lead integrates findings, opens the PR
                        │
                        ▼
     Class A: .claude/scripts/merge-class-a.sh <PR>     Class B: stop, wait for owner
```

`qa-adversarial` and `security-reviewer` are launched as **parallel** `Agent` calls in one
message, not sequential ones — CLAUDE.md's delegation rule already required separate
invocations; this ADR adds that they should run concurrently for wall-clock reasons, not just
context isolation.

### 2. Multi-session and parallel work

- **Worktrees**, formalized: `git worktree add .claude/worktrees/<slug> -b <branch>`, one
  worktree per genuinely independent concurrent unit of work. Not the default — sequential work
  on one branch in one session remains the default, matching the owner's stated preference for
  bounded session-per-chunk work over parallel marathons. Use a worktree when two units of work
  are actually independent (e.g., a Class B review-fix cycle running while new Class A work
  proceeds), not to parallelize work that has a real dependency between its parts.
  The resume-safety rule this repo already learned the hard way — **diff a worktree's
  uncommitted state against `HEAD` before treating it as new work, since it might be a stale
  revert of something already merged** — is written directly into the
  `autonomous-continuation` Skill (§3) so it's load-bearing procedure, not a memory only one
  session happened to retain.
  `guard-applied-migrations.sh` needs no change to be worktree-safe: it resolves `repo_root`
  from `$CLAUDE_PROJECT_DIR` and scopes `git ls-files` to that root, so it already checks the
  worktree's own history, not the primary tree's.
- **Fresh-context specialist review** (§1) is kept and its parallelism made explicit.
- **Cross-session messaging** (`SendMessage`/`ListAgents`): deliberately **not** adopted as a
  handoff mechanism between autonomy sessions. This is a single-owner project running
  session-per-chunk, not a swarm of concurrently coordinating sessions — the durable handoff
  surface that actually matters (PR descriptions, commit messages, GitHub issues, ADRs, memory)
  already survives session boundaries via git, which a live in-process message would not add
  anything to. `SendMessage` stays available for the narrow case of the owner deliberately
  running two interactive sessions side by side; no process is built around it.
- **Headless and scheduled entry points**: `claude -p "<prompt>"` for a locally-triggered
  non-interactive run; a cloud scheduled routine (the `schedule` Skill, what the existing
  runbook calls `RemoteTrigger`) for a run that must happen even when the owner's machine is
  off. Both point at the same `autonomous-continuation` Skill and must never restate its
  contract inline — one source of truth, so the contract only changes in one place. This ADR
  does not create an actual scheduled routine; whether to run one at all, and on what cadence,
  is the owner's decision to make once, not a side effect of a workflow-tooling change.

### 3. Skills (`.claude/skills/`)

Five Skills, each turning a checklist a session currently has to reconstruct from scattered
prose into a single load-bearing procedure:

| Skill | Consolidates | Mandatory when |
|---|---|---|
| `autonomous-continuation` | Handoff §32/§34, ADR-017, the old runbook's "what a session does" section | Any session asked to continue the milestone backlog — interactive, resumed, or scheduled |
| `safe-migration` | `database` agent's migration checklist, handoff §4.9/§7 | Authoring or reviewing any Alembic migration |
| `plaid-sync-review` | `qa-adversarial`'s Plaid attack list, `database`'s sync-semantics section, ADR-003/012 | Any change touching `src/finance_app/plaid/**` or sync semantics |
| `release-readiness` | `sre-release`'s release sequence, `docs/deployment.md`, known unresolved findings from prior deploy-topology review | Reviewing a Milestone 7/8/9 deploy-topology PR, or before an owner-performed `finops deploy` |
| `pre-merge-review` | Nothing existed for this — new mandatory gate | Every unit of work, before a PR is opened or marked ready, Class A included |

`pre-merge-review` is the direct fix for gap 4. It does not reimplement diff review — it wraps
the native `/code-review` Skill with this project's policy: **correctness findings block, every
other finding (simplification, reuse, efficiency) is explicitly optional and non-blocking.** If
the session invoking it is the same context that authored the diff, it must dispatch the review
as a separate `Agent` invocation rather than reading its own diff in the same context that wrote
it — a second pass in the same context is not fresh eyes, whatever it's called.

### 4. Hooks (`.claude/hooks/`)

- **`guard-applied-migrations.sh`** — kept verbatim. Already correct, already worktree-safe
  (§2). Not a redesign target; it's exactly the kind of mechanical enforcement this ADR wants
  more of.
- **`python-checks.sh`** — fixed. `uv` resolution now checks `PATH`, then
  `$HOME/.local/bin/uv`, then `$HOME/.cargo/bin/uv` before giving up. If none resolve, the hook
  prints a loud, impossible-to-miss stderr line stating that local lint verification did **not**
  run and that CI (`.github/workflows/ci.yml`) is the actual gate — never a silent `exit 0`
  indistinguishable from "checked, clean." `pyright` is deliberately not added to this
  per-edit hook: it's slow enough to be poor per-edit feedback, CI already runs it on every PR,
  and (verified in this sandbox) `pyright-python`'s bundled Node binary is currently broken here
  on a missing `libatomic.so.1` — a local-environment gap (see "Dependencies," below), not a
  reason to make every edit hook depend on a working Node install.
- **`guard-protected-branch.sh`** (new) — `PreToolUse` on `Bash`. Mechanically blocks `git
  commit` while the current branch is `main`, and any `git push` whose target resolves to
  `main`. This operationalizes a rule that has been prose-only since Milestone 0 despite being
  load-bearing: CLAUDE.md's working agreement is "branch per unit of work; PR into `main`. No
  direct commits to `main`," yet `git commit` and (after this ADR) `git push` both sit in
  `settings.json`'s unconditional `allow` list. Nothing previously stopped a session from
  committing straight to `main` other than remembering not to.

### 5. `settings.json`

- **New deny entries**: `Read` denies for `*.p12`, `*.credential`, and `credentials.json`.
  `.gitignore` already treats these as secret-shaped (never committed); `settings.json` never
  matched that — a real file matching one of those patterns was readable even though it could
  never be committed. Closes the gap.
- **Broadened existing `.env*` denies** from exact top-level paths (`./.env`) to recursive globs
  (`./**/.env`) so a worktree's copy of the tree (`.claude/worktrees/<slug>/.env`) is covered
  identically to the primary one, now that worktrees are a documented convention rather than an
  incidental one.
- **New allow entries — the routine autonomous path**: `gh pr create/view/checks/list/diff/edit`,
  `gh issue create/list/view/comment`, `gh run list/view`, `git push` (non-force; force-push is
  already denied globally), `git fetch`, `git pull`, `git branch`, `git worktree`, and exactly
  one new scoped command: `.claude/scripts/merge-class-a.sh`.
- **`gh pr merge` stays in `ask`.** It is not the sanctioned merge path and this ADR does not
  loosen it — `.claude/scripts/merge-class-a.sh` is the only allowlisted way from an autonomous
  session to an actual merge, specifically because it re-verifies mechanically rather than
  trusting the calling session's self-classification (§6).
- **Everything gating real production/destructive surface is unchanged**: `docker compose -f
  deploy/compose.yaml`, `deploy/scripts/finops.sh`, `deploy/scripts/with-production-env.sh`,
  `alembic downgrade` (both bare and `uv run` forms), `dropdb`, `psql` all stay in `ask`. This
  ADR does not loosen any of them.

### 6. The Class-A merge gate: `.claude/scripts/merge-class-a.sh`

The one new allowlisted command from §5. Given a PR number, it independently re-verifies three
things before invoking `gh pr merge --squash --delete-branch`, and refuses (exit non-zero,
merges nothing) if any fail:

1. **`gh pr view --json mergeable`** is exactly `MERGEABLE` — no conflicts.
2. **Every reported check is `SUCCESS` or `SKIPPED`, and at least five were reported** — the
   count floor exists because an empty or too-small check list means CI hasn't populated yet,
   and ADR-017 is explicit: never merge on red, pending, *or a missing check*. `SKIPPED` is
   accepted because this repository's own CI intentionally skips CD-stage jobs
   (`publish-image`, `migration-preflight`, `staging-smoke`, `production-deploy`) on `pull_request`
   events — they only run on push to `main` — so requiring `SUCCESS` on those would make no PR
   ever mergeable.
3. **The diff does not touch a path this repository treats as inherently non-Class-A**:
   `migrations/versions/`, `deploy/`, `.claude/`, `docs/adr/`, `src/finance_app/plaid/`,
   `src/finance_app/agent/`, or the files `CLAUDE.md`, `CLAUDE_FINANCE_APP_HANDOFF.md`,
   `docs/security-model.md`. This is a mechanical safety **net**, not a substitute for correct
   classification at authoring time — a session should never reach for this script believing a
   change is Class B. But self-classification can be wrong, and this catches the case where it
   is, independent of whatever the authoring session believed. It is also why *this very PR* —
   which touches `.claude/**` and `docs/adr/**` by definition — cannot auto-merge through this
   script regardless of how trivial any individual line in it looks, which is exactly the
   property ADR-017's "no one holds authority over widening its own authority" needs.

This is the concrete answer to gap 2: unattended Class A merges no longer stall on `ask`, and
they no longer depend on the calling session's judgment being the only check.

### 7. Context discipline (added to `CLAUDE.md`)

- **What must survive compaction**: current branch, modified files, open PR links/numbers, this
  unit of work's risk classification, and the exact test commands already run. Practically,
  these are re-queried from git/`gh` (the actual source of truth) rather than trusted from
  memory — `git status`/`git diff`/`gh pr list`/`gh pr view` are always authoritative over
  recollection, before or after compaction.
- **Research delegated to subagents/forks**, not read wholesale into the lead session's context
  — matches the `Agent` tool's own fork guidance (fork what you won't need to keep).
- **Plan mode** for multi-file or uncertain work; **direct execution** for a single-file,
  well-scoped, low-risk change. Don't plan-mode a one-line fix; don't skip planning a
  multi-module change because a session is in a hurry.
- **Fresh-context correctness review is mandatory before "done"** — `pre-merge-review` (§3),
  scoped to correctness findings only. Every other finding class is explicitly optional.

### 8. Session lifecycle

- **Boot**: `CLAUDE.md` loads automatically. A session picking up the backlog invokes
  `autonomous-continuation`, which reads `README.md`'s Status line, `git status`, `git branch
  --show-current`, `gh pr list --state open`, and cross-references handoff §29 for the first
  incomplete milestone step. A session given a specific, scoped task by the owner does not need
  this procedure — normal boot (`CLAUDE.md` plus whatever the owner said) is enough.
- **End**: always exactly one of — a merged Class A commit, an open Class B PR, or a durable
  note (PR description, commit message, or `gh issue create`) explaining a blocker. Never an
  idle wait, never a retry loop hoping a blocker resolves itself — unchanged from handoff §32,
  now with `gh issue`/`gh pr edit` allowlisted (§5) so leaving that note doesn't itself stall on
  a permission prompt nobody is present to answer.
- **MCP**: none added. Every candidate this ADR considered is already covered by the native
  toolset without a new trust surface — see "What we deliberately did not add."

## Consequences

- Unattended Class A autonomy is now real rather than aspirational: the merge no longer depends
  on a permission prompt nobody answers, and the mechanism that replaces that prompt is stricter
  than the prompt was (a path-based safety net the prompt never had).
- Every unit of work, Class A included, gets one fresh-context correctness pass before it's
  called done — previously only Class B did.
- The five Skills are load-bearing documentation: letting one drift out of date now has the same
  cost that letting an agent prompt drift had before this ADR (gap 1). Whoever next changes the
  migration checklist, the Plaid sync attack list, or the release sequence must update the
  corresponding Skill in the same change, not just the narrative doc it was copied from.
- `guard-protected-branch.sh` adds friction to exactly one thing: committing or pushing directly
  to `main`. Every other git operation is unaffected.
- This PR is, by its own §6 path screen, permanently ineligible for the mechanism it introduces
  — it must be merged by the owner, by hand, every time a future change like it lands.

## What we deliberately did not add (and why)

- **A dedicated webhook-review agent** (§1) — folds into three existing specialists; a new agent
  per milestone deliverable fragments review rather than deepening it.
- **A bespoke code-review agent** (§1, §3) — native `/code-review`, wrapped by a policy Skill,
  already provides fresh-context correctness review without a second implementation to maintain.
- **MCP servers of any kind.** Considered: a GitHub MCP server (rejected — `gh` CLI already
  covers every operation this workflow needs, is already permission-scoped in `settings.json`,
  and a second GitHub integration would be a second trust surface for the same capability);
  a documentation-search MCP server (rejected — `WebFetch`/`WebSearch` already cover the "verify
  current docs" requirement in handoff §32 natively); a production-observability MCP server
  (rejected outright — handoff §15's own reasoning holds regardless of the waiver: `finops` is
  the narrow interface by design, and a generic observability MCP would be a wider one).
- **Cross-session live messaging as a primary handoff mechanism** (§2) — git-durable artifacts
  already survive session boundaries; this is a single-owner, session-per-chunk project, not a
  coordinating swarm.
- **A second orchestration framework or service** — native subagents, Skills, hooks, and CI have
  not proven insufficient. The waiver removed handoff §15's blanket avoidance policy; it did not
  invalidate the underlying test ("prove native tooling insufficient first"), which this change
  still passes.
- **Branch-protection-equivalent GitHub configuration** — not available on this plan (confirmed
  403 on the protection API; unchanged finding from ADR-017).

## Mapping: old → new

| Old | Disposition |
|---|---|
| Handoff §12 (org chart) | Roster kept, refreshed — §1 |
| Handoff §14 (no Skills until proven manually) | Waived 2026-09-12; five Skills ship in this change — §3 |
| Handoff §15 (avoid MCP; no second orchestration layer) | Waived; no MCP added anyway, each candidate reasoned through — "What we deliberately did not add" |
| ADR-017's Class A/B/C authority | Unchanged — this ADR gives it a mechanical merge gate it lacked (§6) |
| `gh pr merge` in `settings.json`'s `ask` | Stays in `ask`; `.claude/scripts/merge-class-a.sh` is the new sanctioned path — §5, §6 |
| `python-checks.sh`'s silent `uv` no-op | Fixed detection plus an honest, loud skip message — §4 |
| No mechanical backing for "no direct commits to `main`" | `guard-protected-branch.sh` — §4 |
| Ad hoc `.claude/worktrees/` usage | Documented convention with the diff-against-`HEAD` resume rule — §2 |
| `docs/runbooks/autonomous-continuation.md` restating the ADR-017 contract | Slimmed to the owner-facing post-hoc checklist it was always meant to be; session-behavior procedure now lives once, in the `autonomous-continuation` Skill |
| `architecture.md`'s "OpenAI-backed runtime agent" | Corrected to provider-interchangeable language — §1 |
| `qa-adversarial.md`'s thin "Infrastructure attacks" section | Expanded with concrete findings from the PR #13 round-3 review plus a webhook subsection — §1 |

## Dependencies for a fully working local environment

Not new project dependencies — environment gaps found while building this ADR, listed so they
don't have to be rediscovered:

- **`uv`** is already present in this sandbox at `~/.local/bin/uv` and works correctly when
  invoked through a normal interactive shell (confirmed: `ruff format --check .` and `ruff
  check .` both pass against the current tree). It is **not** reliably on `PATH` for a hook
  subprocess launched with a minimal environment — `python-checks.sh`'s fallback resolution
  (§4) exists specifically for that gap; no installation action is needed, only the hook fix
  already made.
- **`pyright`** (via `pyright-python`, which bundles Node) fails in this sandbox with `error
  while loading shared libraries: libatomic.so.1: cannot open shared object file`. Installing
  `libatomic1` (Debian/Ubuntu package name; provides `libatomic.so.1`) would fix local `pyright`
  runs. Not required for this change to be safe to merge: CI's `ubuntu-latest` runners are
  unaffected (this repo's CI already runs `pyright` successfully on every PR), and `pyright` was
  deliberately kept out of the per-edit hook (§4) regardless of this gap.

## Revisit when

- This repository's GitHub plan changes and real branch protection becomes available — the
  scripted merge gate (§6) becomes a second independent layer rather than the only one.
- Milestone 9 defines automatic-deployment rollback criteria — revisit whether any form of
  scripted gating should extend toward deploy automation. ADR-017's "merge is never deploy"
  holds regardless; this would only ever be about strengthening the merge-side gate further.
- A specialist's checklist goes stale again (§1's gap 1 is expected to recur as milestones land)
  — the fix each time is updating that agent's prompt and the Skill it's paired with, not
  restructuring the roster again.
