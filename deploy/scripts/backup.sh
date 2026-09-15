#!/bin/sh
# Create an encrypted PostgreSQL backup (handoff §22, ADR-015). Invoked by
# finance-backup.service. All the actual logic (pg_dump as finance_backup,
# GPG symmetric encryption, sha256, ops.backup_runs bookkeeping) lives in
# `finance_app.ops.backup`, run inside the already-built image's `backup`
# Compose service (finding 1: finance_backup/BACKUP_ENCRYPTION_KEY are
# scoped to that one-shot service, never the long-running `app` service)
# so it always uses the exact same code as the release currently deployed.
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"

exec docker compose -f "$COMPOSE_FILE" --profile backup run --rm -T backup \
    python -m finance_app.ops.backup backup
