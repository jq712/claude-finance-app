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
# 32 bytes of real randomness, not a guessable PID-derived string (QA-18)
# — this scratch instance briefly shares the production Docker network so
# a weak password here would be a real, if narrow, exposure window.
SCRATCH_PASSWORD="$(openssl rand -hex 32)"
NETWORK="${FINANCE_APP_NETWORK:-finance-app}"

cleanup() {
    docker rm -f "$SCRATCH_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# `set -e` alone does NOT abort a `VAR="$(cmd)"` assignment before this
# `if` gets a chance to run its own error message (QA-18) — the explicit
# `if ! LATEST=...` form checks the exit code directly instead of relying
# on -e to short-circuit the whole script with no explanation.
if ! LATEST="$(docker compose -f "$COMPOSE_FILE" run --rm -T app python -m finance_app.ops.backup latest)"; then
    echo "restore-verify: no successful backup found to verify" >&2
    exit 1
fi
if [ -z "$LATEST" ]; then
    echo "restore-verify: no successful backup found to verify" >&2
    exit 1
fi
BACKUP_RUN_ID="${LATEST%%	*}"
BACKUP_PATH="${LATEST#*	}"

# POSTGRES_PASSWORD_FILE (not POSTGRES_PASSWORD) so the scratch instance's
# password isn't sitting in this container's `environment:`/`docker
# inspect` output for no reason beyond convenience — same argv/env-
# exposure class as finding 2/QA-16. A private, 0600 temp file, removed
# once the scratch container has started and read it.
SCRATCH_PASSWORD_FILE="$(mktemp)"
chmod 600 "$SCRATCH_PASSWORD_FILE"
printf '%s' "$SCRATCH_PASSWORD" > "$SCRATCH_PASSWORD_FILE"

docker run -d --name "$SCRATCH_CONTAINER" \
    --network "$NETWORK" \
    -e POSTGRES_USER=finance_migrator \
    -e POSTGRES_PASSWORD_FILE=/run/secrets/scratch_password \
    -e POSTGRES_DB=finance_restore_drill \
    -v "$SCRATCH_PASSWORD_FILE:/run/secrets/scratch_password:ro" \
    postgres:17-alpine >/dev/null
rm -f "$SCRATCH_PASSWORD_FILE"

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

# TARGET_URL (which embeds the scratch password) is passed via --target-url
# below. That's still a CLI argument, but to `docker compose run` inside a
# throwaway restore-drill container talking to a throwaway scratch
# database that's destroyed at the end of this script either way — a
# materially smaller exposure than a production credential, and
# `finance_app.ops.backup`'s own pg_dump/pg_restore calls (the ones that
# matter — finding 2) no longer put any password on argv regardless of
# how this target URL was obtained.
docker compose -f "$COMPOSE_FILE" run --rm -T app \
    python -m finance_app.ops.backup restore "$BACKUP_PATH" --target-url "$TARGET_URL" \
        --backup-run-id "$BACKUP_RUN_ID"

docker compose -f "$COMPOSE_FILE" run --rm -T app \
    python -m finance_app.ops.backup verify "$BACKUP_RUN_ID" --target-url "$TARGET_URL"
