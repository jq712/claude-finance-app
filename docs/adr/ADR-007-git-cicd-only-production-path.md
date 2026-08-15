# ADR-007: Git and CI/CD are the only normal path to production

**Status:** Accepted

## Context

An autonomous engineering agent with shell access to a production host holding real financial data is an unbounded risk. The failure mode is not malice — it is a confident, incorrect action taken quickly with no reviewable record.

## Decision

Code reaches production only as an immutable image built by CI from a committed revision. No editing on the VPS. No self-hosted runner on the VPS. Production diagnosis goes through the narrow `finops` interface, not ad hoc shell or `psql`. Emergency fixes still land in Git first.

## Consequences

- Every production change has a commit, a review, a CI record, and a rollback target.
- `.claude/settings.json` denies `ssh`/`scp`/`rsync` so this holds mechanically, not just by instruction.
- Emergency response is slower by the length of a CI run. Accepted deliberately.
- If `finops` lacks a needed signal, the fix is to add the command, not to reach around the interface.

## Revisit when

Never.
