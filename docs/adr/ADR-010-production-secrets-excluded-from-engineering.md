# ADR-010: Production secrets are excluded from the engineering environment

**Status:** Accepted

## Context

The Plaid access token is the single most dangerous artifact in the system: it grants ongoing read access to the owner's complete financial history. Any environment that holds it must be treated as production.

## Decision

Production secret material — Plaid client ID and secret, Plaid access token, OpenAI runtime key, PostgreSQL credentials, webhook secret, backup encryption key — exists only inside the production runtime boundary, as systemd encrypted credentials. Never in the repository, never in CI, never in a Claude Code session context.

## Consequences

- Enforced mechanically by `.claude/settings.json` deny rules on credential paths and on `ssh`/`scp`/`rsync`, not only by instruction.
- A hook additionally blocks agent edits to `.env`, `secrets/`, `*.pem`, and `*.key`.
- Tasks that appear to need production credentials are design errors and must be escalated, not worked around.
- Credential rotation is an owner-performed runbook, not an automated one.

## Revisit when

Never.
