# TASKS.md — orchestrator task queue

Owner-maintained. Tracked on `main`. Strictly read-only to the orchestrator (AGENTS.md §1 item 5
and §7): it never adds, reorders, rewords, marks or otherwise modifies an entry. Task completion,
blocking and attempt history live in `.orchestrator/STATUS.md` only.

The orchestrator selects exactly one task per iteration: the first eligible entry in file order
(AGENTS.md §7). A malformed entry is skipped and recorded, never repaired.

## Entry schema (AGENTS.md §7)

    id:             <slug>
    title:          <one line>
    allowed_paths:  <explicit list, ≤5 paths, no glob wider than one directory>
    done_when:      <checkable condition>
    depends_on:     <task ids | none>
    conflict_keys:  <keys | none>
    tests:          <commands | none>          # optional
    class_hint:     A | B                      # optional, never authoritative

## Entries

No entries yet. Starter tasks are an owner decision (see IMPLEMENTATION-PLAN.md, Open Questions).
