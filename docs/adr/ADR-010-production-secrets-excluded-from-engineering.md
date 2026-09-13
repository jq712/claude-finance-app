# ADR-010: Production secrets are excluded from the engineering session

**Status:** Accepted, revised 2026-09-13

## Context

The Plaid access token is the single most dangerous artifact in the system: it grants ongoing read access to the owner's complete financial history. Any environment that holds it must be treated as production.

**Revision, 2026-09-13:** "excluded from the engineering environment" originally meant excluded from a physically separate host with no network path to production. The owner has since deliberately chosen to run the engineering workspace and the production deployment on one shared VPS. The title now says "engineering *session*" rather than "environment" on purpose: what's excluded is the credential material reaching this session's process, filesystem view, or environment variables — not a claim about the underlying hardware, which the two now share. See docs/security-model.md's "Trust boundaries" for the full revised picture and ADR-007 for the parallel revision to the release-path ADR this one has always paired with.

## Decision

Production secret material — Plaid client ID and secret, Plaid access token, runtime agent provider API key(s) (OpenAI and/or Anthropic, per ADR-014), PostgreSQL credentials, webhook secret, backup encryption key — exists only inside the production runtime boundary, as systemd encrypted credentials, decrypted only inside a scoped systemd unit for the duration of one command (`deploy/scripts/with-production-env.sh`). Never in the repository, never in CI, never decrypted into a Claude Code session's process environment or readable by a Claude Code session's filesystem access — regardless of which host that session happens to be running on.

## Consequences

- **What changed:** on a shared host, "never in a Claude Code session context" can no longer mean "no network route exists." It now means: the credential files (`/etc/finance-app/credentials/*.cred`, chmod 700) and the production directory (`/opt/finance-app`) are owned by a Unix user distinct from whichever user runs Claude Code sessions, that user is not `sudo`-capable, and `systemd-creds`' TPM/machine-key-bound decryption is invoked only from inside the scoped systemd units the runbook defines — never interactively, never by this session.
- Still enforced by `.claude/settings.json` deny rules on credential paths and on `ssh`/`scp`/`rsync`/`systemd-creds` — real, but now policy-level enforcement on a shared machine rather than the network-level enforcement this ADR originally described. Treat it as defense-in-depth on top of the Unix-permission boundary, not a replacement for it.
- A hook additionally blocks agent edits to `.env`, `secrets/`, `*.pem`, and `*.key`.
- Tasks that appear to need production credentials are design errors and must be escalated, not worked around.
- Credential rotation, minting, and every `systemd-creds`/`systemd-run` invocation remain an owner-performed runbook action — the shared host does not change who is allowed to touch credential material, only where their session happens to run from.

## Revisit when

Already was (2026-09-13, this revision). Revisit again if the production credential files or directory are ever found to be readable by the Claude Code session's Unix user, or if that user gains `sudo` — either means this ADR's actual protection no longer exists and the decision needs remaking, not patching.
