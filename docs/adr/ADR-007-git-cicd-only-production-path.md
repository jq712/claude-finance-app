# ADR-007: Git and CI/CD are the only normal path to production

**Status:** Accepted, revised 2026-09-13

## Context

An autonomous engineering agent with shell access to a production host holding real financial data is an unbounded risk. The failure mode is not malice — it is a confident, incorrect action taken quickly with no reviewable record.

**Revision, 2026-09-13:** this ADR originally assumed engineering and production ran on physically separate hosts, with no network path between them. The owner has since deliberately chosen to run both on one shared VPS (ADR-019: bare-metal production at `/opt/finance`, no Docker). That changes *which* protection is doing the work — a directory/Unix-user/tool-policy boundary on one host instead of a network gap; see `docs/security-model.md`'s "Trust boundaries" and ADR-019 for the full picture — but the underlying concern this ADR names is unchanged, and sharper now that "shell access to a production host" describes every Claude Code session in this repo, not a hypothetical.

## Decision

Code reaches production only through the release flow ADR-019 defines — a known, CI-green git ref copied into `/opt/finance` by the owner — never a local edit or an ad hoc copy of the dev tree's working state. No editing `/opt/finance` directly, from this session or any other. No self-hosted CI runner on the VPS (a GitHub Actions runner would itself need broad host access — a different and larger risk than the engineering session sharing the box). Production diagnosis goes through the narrow `finops` interface, not ad hoc shell or `psql`. Emergency fixes still land in Git first.

## Consequences

- Every production change has a commit, a review, a CI record, and a rollback target.
- **What no longer holds mechanically:** this session has a local shell on the same machine production runs on. `.claude/settings.json` still denies `ssh`/`scp`/`rsync`/`systemd-creds` and blocks reads of `/etc/finance-app/**`/`/opt/finance`, but those are this session's own configured policy, not a network gap.
- **What still holds, and why it's real:** `/opt/finance` is owned by a Unix user this session's user is not, and this session has no `sudo` — an OS-enforced permission check, independent of Claude Code's own policy. See ADR-019 and `docs/security-model.md` for what this depends on staying true (in particular: this session's user must never gain Docker-group-equivalent or other root-adjacent access — moot for now since there is no Docker, but worth remembering if that ever changes).
- **New, accepted risk:** shared-host resource exhaustion or an errant destructive command in the engineering workspace can degrade production availability without ever touching a credential. Categorically impossible under host separation; accepted, not defended against.
- Emergency response is slower by the length of a CI run. Accepted deliberately.
- If `finops` lacks a needed signal, the fix is to add the command, not to reach around the interface.

## Revisit when

Already was (2026-09-13, this revision). Revisit again only if the Unix-permission boundary between the engineering user and `/opt/finance` is ever found not to hold in practice.
