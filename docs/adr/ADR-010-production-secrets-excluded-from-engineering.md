# ADR-010: Production secrets are excluded from the engineering session

**Status:** Accepted, revised 2026-09-13

## Context

The Plaid access token is the single most dangerous artifact in the system: it grants ongoing read access to the owner's complete financial history. Any environment that holds it must be treated as production.

**Revision, 2026-09-13:** "excluded from the engineering environment" originally meant excluded from a physically separate host. The owner has since deliberately chosen to run the engineering workspace and the production deployment (bare-metal at `/opt/finance`, per ADR-019 — no Docker) on one shared VPS. The title now says "engineering *session*" rather than "environment" on purpose: what's excluded is the credential material reaching this session's process, filesystem view, or environment variables — not a claim about the underlying hardware, which the two now share.

## Decision

Production secret material — Plaid client ID and secret, Plaid access token, runtime agent provider API key(s) (OpenAI and/or Anthropic, per ADR-014), `finance_prod` PostgreSQL credentials, webhook secret, backup encryption key — exists only inside `/opt/finance/.env` (mode 600) and as systemd encrypted credentials for anything a systemd unit needs decrypted at start. Never in the repository, never in CI, never decrypted into a Claude Code session's process environment or readable by a Claude Code session's filesystem access — regardless of which host that session happens to run on.

## Consequences

- **What changed:** on a shared host, "never in a Claude Code session context" can no longer mean "no network route exists." It now means: `/opt/finance` (including `/opt/finance/.env`) is owned by a Unix user distinct from whichever user runs Claude Code sessions, that user is not `sudo`-capable, and any `systemd-creds`-backed credential is decrypted only inside the scoped systemd unit that needs it — never interactively, never by this session. See ADR-019 for the full directory/user layout and `docs/security-model.md` for the threat model this depends on.
- Still enforced by `.claude/settings.json` deny rules on credential paths and on `ssh`/`scp`/`rsync`/`systemd-creds` — real, but policy-level enforcement on a shared machine now, not network-level. Defense-in-depth on top of the Unix-permission boundary, not a replacement for it.
- A hook additionally blocks agent edits to `.env`, `secrets/`, `*.pem`, and `*.key`.
- Tasks that appear to need production credentials are design errors and must be escalated, not worked around.
- Credential rotation, minting, and every privileged production command remain an owner-performed runbook action — the shared host does not change who is allowed to touch credential material, only where their session happens to run from.

## Revisit when

Already was (2026-09-13, this revision). Revisit again if `/opt/finance` or its `.env` are ever found readable by the Claude Code session's Unix user, or if that user gains `sudo` or equivalent (e.g. Docker-group membership, if Docker is ever reintroduced) — either means this ADR's actual protection no longer exists.
