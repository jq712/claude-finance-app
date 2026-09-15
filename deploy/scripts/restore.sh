#!/bin/sh
# Restore an encrypted backup into a target database (handoff §22).
#
# Usage: restore.sh <encrypted-backup-path> [target-database-url] [backup-run-id]
#
# `target-database-url` may be given positionally (fine for a throwaway
# scratch/drill database — no real credential) or via $TARGET_DATABASE_URL
# (preferred whenever it embeds a real credential, e.g. restoring over
# production — a positional argument lands in shell history, finding 2 /
# docs/runbooks/restore-backup.md). `backup-run-id`, similarly positional
# or $BACKUP_RUN_ID, is optional but recommended: when given, the
# artifact's recorded sha256 is verified against the file on disk before
# it is decrypted (finding 7 / QA-19).
#
# `target-database-url` must be a scratch/disaster-recovery database, never
# production's primary use — `pg_restore --clean` drops existing objects
# first. This is an owner-performed, deliberately manual operation (see
# docs/runbooks/restore-backup.md) — it is not on a timer, unlike backup.sh
# and restore-verify.sh.
set -eu

if [ "$#" -lt 1 ]; then
    echo "usage: restore.sh <encrypted-backup-path> [target-database-url] [backup-run-id]" >&2
    echo "  target-database-url: positional, or \$TARGET_DATABASE_URL (preferred if it" >&2
    echo "                       embeds a real credential -- positional args land in" >&2
    echo "                       shell history)" >&2
    echo "  backup-run-id:       positional, or \$BACKUP_RUN_ID (optional; enables a" >&2
    echo "                       sha256 check against the recorded value before decrypting)" >&2
    exit 2
fi

BACKUP_PATH="$1"
TARGET_URL="${TARGET_DATABASE_URL:-${2:-}}"
BACKUP_RUN_ID="${BACKUP_RUN_ID:-${3:-}}"

if [ -z "$TARGET_URL" ]; then
    echo "restore.sh: no target database URL given (positional \$2 or \$TARGET_DATABASE_URL)" >&2
    exit 2
fi

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"

if [ -n "$BACKUP_RUN_ID" ]; then
    exec docker compose -f "$COMPOSE_FILE" --profile backup run --rm -T backup \
        python -m finance_app.ops.backup restore "$BACKUP_PATH" --target-url "$TARGET_URL" \
            --backup-run-id "$BACKUP_RUN_ID"
fi

exec docker compose -f "$COMPOSE_FILE" --profile backup run --rm -T backup \
    python -m finance_app.ops.backup restore "$BACKUP_PATH" --target-url "$TARGET_URL"
