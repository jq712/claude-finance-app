#!/bin/sh
# Create an encrypted PostgreSQL backup (handoff §22, ADR-015). Invoked by
# finance-backup.service. All the actual logic (pg_dump as finance_backup,
# GPG symmetric encryption, sha256, ops.backup_runs bookkeeping) lives in
# `finance_app.ops.backup`, run inside the already-built `app` image so it
# always uses the exact same code as the release currently deployed.
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"

exec docker compose -f "$COMPOSE_FILE" run --rm -T app python -m finance_app.ops.backup backup
