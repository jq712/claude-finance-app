#!/bin/sh
# Weekly restore drill (handoff §22, ADR-015; sre-release persona: "a
# backup that has never been restored is not verified"). Restores the most
# recent successful backup into a throwaway PostgreSQL container — never
# production, never even the real dev/staging database — runs
# `finance_app.ops.backup`'s post-restore sanity checks against it, and
# records the result in `ops.backup_runs` via the *production* app/postgres
# (linked to the backup it verified), so `finops backup-status` reflects
# it. The throwaway container is always removed, success or failure.
#
# Invoked by finance-restore-drill.timer (weekly). Safe to run by hand.
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"
SCRATCH_CONTAINER="finance-restore-drill-$$"
SCRATCH_PASSWORD="restore-drill-scratch-$$"
NETWORK="${FINANCE_APP_NETWORK:-finance-app}"

cleanup() {
    docker rm -f "$SCRATCH_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

LATEST="$(docker compose -f "$COMPOSE_FILE" run --rm -T app python -m finance_app.ops.backup latest)"
if [ -z "$LATEST" ]; then
    echo "restore-verify: no successful backup found to verify" >&2
    exit 1
fi
BACKUP_RUN_ID="${LATEST%%	*}"
BACKUP_PATH="${LATEST#*	}"

docker run -d --name "$SCRATCH_CONTAINER" \
    --network "$NETWORK" \
    -e POSTGRES_USER=finance_migrator \
    -e POSTGRES_PASSWORD="$SCRATCH_PASSWORD" \
    -e POSTGRES_DB=finance_restore_drill \
    postgres:17-alpine >/dev/null

echo "restore-verify: waiting for scratch instance ${SCRATCH_CONTAINER} to accept connections..." >&2
i=0
until docker exec "$SCRATCH_CONTAINER" pg_isready -U finance_migrator -d finance_restore_drill \
        >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        echo "restore-verify: scratch instance never became ready" >&2
        exit 1
    fi
    sleep 2
done

TARGET_URL="postgresql+psycopg://finance_migrator:${SCRATCH_PASSWORD}@${SCRATCH_CONTAINER}:5432/finance_restore_drill"

docker compose -f "$COMPOSE_FILE" run --rm -T app \
    python -m finance_app.ops.backup restore "$BACKUP_PATH" --target-url "$TARGET_URL"

docker compose -f "$COMPOSE_FILE" run --rm -T app \
    python -m finance_app.ops.backup verify "$BACKUP_RUN_ID" --target-url "$TARGET_URL"
