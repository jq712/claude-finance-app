# ADR-007: Git and CI/CD are the only normal path to production

**Status:** Accepted, revised 2026-09-13

## Context

An autonomous engineering agent with shell access to a production host holding real financial data is an unbounded risk. The failure mode is not malice — it is a confident, incorrect action taken quickly with no reviewable record.

**Revision, 2026-09-13:** this ADR originally assumed engineering and production ran on physically separate hosts, with no network path between them. The owner has since deliberately chosen to run both on one VPS (cost/convenience), in separate directories under separate Unix users. That changes *which* protection is doing the work — see docs/security-model.md's "Trust boundaries" for the full picture — but the underlying concern this ADR names (an agent taking a confident, unreviewed, irreversible action against production) is unchanged, and if anything sharper now that "shell access to a production host" describes every Claude Code session in this repo, not a hypothetical.

## Decision

Code reaches production only as an immutable image built by CI from a committed revision — never a local `docker build` of the dev workspace's tree, even though that tree and the production directory now sit on the same disk. No editing the production directory (`/opt/finance-app`) or the deployed containers directly, from this session or any other. No self-hosted CI runner on the VPS (a GitHub Actions runner would itself need broad host access — a different and larger risk than the engineering session sharing the box). Production diagnosis goes through the narrow `finops` interface, not ad hoc shell or `psql`. Emergency fixes still land in Git first.

## Consequences

- Every production change has a commit, a review, a CI record, and a rollback target.
- **What no longer holds mechanically:** this session literally has a local shell on the same machine production runs on. `.claude/settings.json` still denies `ssh`/`scp`/`rsync`/`systemd-creds` and blocks reads of `/etc/finance-app/**`, but those are this session's own configured policy, enforced by Claude Code, not a network gap — a different tool invocation or a misconfigured session is a materially closer failure than it was under host separation.
- **What still holds, and why it's real rather than aspirational:** the production directory and `/etc/finance-app/**` are owned by a Unix user this session's user is not, and this session has no `sudo` (verified: no passwordless sudo entry exists). That is an OS-enforced permission check, independent of Claude Code's own policy — the honest remaining wall.
- **New, accepted risk that has no mitigation:** shared-host resource exhaustion or an errant destructive command in the engineering workspace (disk fill, `docker system prune` without scoping, CPU/memory contention) can degrade production availability without ever touching a credential. This was categorically impossible under host separation. It is now accepted, not defended against.
- **New risk that was found and closed:** `deploy/compose.yaml` sets a fixed top-level `name: finance-app` — a Compose *project* name, not derived from the invoking directory — so running it from the engineering workspace would resolve to the same project as the live production stack (`docker compose -f deploy/compose.yaml down` would stop it, not a harmless local simulation, once production actually exists). `.claude/settings.json` now denies that command outright rather than `ask`-gating it; see `docs/security-model.md`'s threat model. This is the kind of consequence host separation used to make unreachable and now has to be found by inspection instead — worth re-auditing `deploy/compose.yaml` and `.claude/settings.json` together any time either changes.
- Emergency response is slower by the length of a CI run. Accepted deliberately.
- If `finops` lacks a needed signal, the fix is to add the command, not to reach around the interface.

## Revisit when

Already was (2026-09-13, this revision). Revisit again only if the Unix-permission boundary between the engineering user and the production directory/credentials is ever found to not hold in practice (e.g. group membership drift, a `sudo` entry added for convenience) — that would mean this ADR's remaining protection is gone and the decision needs to be re-made from scratch, not patched.
