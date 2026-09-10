#!/bin/sh
# Restore an encrypted backup into a target database (handoff §22).
#
# Usage: restore.sh <encrypted-backup-path-inside-the-backups-volume> <target-database-url>
#
# `target-database-url` must be a scratch/disaster-recovery database, never
# production — `pg_restore --clean` drops existing objects first. This is
# an owner-performed, deliberately manual operation (see
# docs/runbooks/restore-backup.md) — it is not on a timer, unlike backup.sh
# and restore-verify.sh.
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: restore.sh <encrypted-backup-path> <target-database-url>" >&2
    exit 2
fi

BACKUP_PATH="$1"
TARGET_URL="$2"
COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"

exec docker compose -f "$COMPOSE_FILE" run --rm -T app \
    python -m finance_app.ops.backup restore "$BACKUP_PATH" --target-url "$TARGET_URL"
