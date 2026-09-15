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
# Invoked by finance-restore-drill.timer (weekly). Safe to run by hand
# outside systemd too — see the $RUNTIME_DIRECTORY fallback below.
#
# ADR-016 D7: the scratch password file used to be staged with a bare
# `mktemp` (i.e. under `/tmp`) and bind-mounted into the scratch Postgres
# container by path. `finance-restore-drill.service` sets `PrivateTmp=
# true`, which namespaces `/tmp`/`/var/tmp` for this script's own
# processes — but the bind-mount source is resolved by the Docker
# *daemon*, which runs outside that namespace and cannot see a path
# under this unit's private `/tmp`. The daemon silently created an empty
# *directory* there instead of finding the file, so
# `POSTGRES_PASSWORD_FILE` pointed at a directory, the scratch Postgres
# never started, and the drill failed every week (QA-25). `systemd`'s
# `RuntimeDirectory=` is created in the *host* mount namespace (visible
# to the daemon) and removed automatically when the unit stops — the
# password file is staged there instead, and the *directory* is what
# gets bind-mounted (a directory bind mount survives inode replacement;
# a single-file one does not, which is the other half of what produced
# this defect).
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"
SCRATCH_CONTAINER="finance-restore-drill-$$"
# 32 bytes of real randomness, not a guessable PID-derived string (QA-18)
# — this scratch instance briefly shares the production Docker network so
# a weak password here would be a real, if narrow, exposure window.
SCRATCH_PASSWORD="$(openssl rand -hex 32)"
NETWORK="${FINANCE_APP_NETWORK:-finance-app}"

# Fail loudly rather than silently falling back to `/tmp` when running
# under systemd (i.e. $CREDENTIALS_DIRECTORY is set, proving this is a
# LoadCredentialEncrypted= unit) but $RUNTIME_DIRECTORY is not — that
# combination means the unit file is missing `RuntimeDirectory=` and
# this script would otherwise silently reproduce QA-25. A bare `mktemp
# -d` fallback is kept only for a manual, non-systemd invocation.
if [ -n "${CREDENTIALS_DIRECTORY:-}" ] && [ -z "${RUNTIME_DIRECTORY:-}" ]; then
    echo "restore-verify.sh: running under systemd (\$CREDENTIALS_DIRECTORY is set) but" >&2
    echo "\$RUNTIME_DIRECTORY is not -- finance-restore-drill.service is missing" >&2
    echo "RuntimeDirectory=finance-app-restore-drill. Refusing to fall back to /tmp," >&2
    echo "which the Docker daemon cannot resolve under PrivateTmp=true (ADR-016 D7)." >&2
    exit 1
fi
if [ -n "${RUNTIME_DIRECTORY:-}" ]; then
    SCRATCH_STAGING_DIR="$RUNTIME_DIRECTORY"
    _MANUAL_STAGING_DIR=""
else
    # Manual, non-systemd invocation only — systemd removes
    # $RUNTIME_DIRECTORY itself when the unit stops, but a directory
    # this script created by hand is this script's own to remove.
    SCRATCH_STAGING_DIR="$(mktemp -d)"
    _MANUAL_STAGING_DIR="$SCRATCH_STAGING_DIR"
fi

cleanup() {
    # Every step here is `|| true`: this runs under `set -e`, and inside
    # the INT/TERM handlers below it runs before `exit 130`/`exit 143` —
    # a failing `rm` would otherwise abort the handler right there, and
    # the script would exit with *that* command's status instead of the
    # signal-derived one, reintroducing the exit-status misattribution
    # QA-39 exists to close (systemd/journald would again see something
    # other than "killed by signal" for a drill actually killed by one).
    docker rm -f "$SCRATCH_CONTAINER" >/dev/null 2>&1 || true
    # Not just belt-and-suspenders for the normal path (which already
    # removes this once the scratch instance is ready) — also the only
    # thing that removes it if the script exits early (readiness timeout,
    # INT/TERM) before that point is ever reached. `RuntimeDirectory=`
    # teardown handles this on the systemd path regardless, but this
    # shouldn't depend on that.
    rm -f "${SCRATCH_PASSWORD_FILE:-}" || true
    if [ -n "$_MANUAL_STAGING_DIR" ]; then
        rm -rf "$_MANUAL_STAGING_DIR" || true
    fi
}
# A bare `trap cleanup EXIT INT TERM` runs `cleanup` on INT/TERM and then
# *returns* — the shell resumes wherever it was interrupted (e.g. the
# `pg_isready` readiness loop) and runs to its own conclusion, driving
# `docker`/re-creating the password file against resources `cleanup` just
# destroyed, and the exit status systemd/journald sees ends up being
# whatever that resumed run produces instead of "killed by signal" — the
# weekly drill's failure is misattributed during exactly the incident
# where it matters (QA-39). `EXIT` needs no exit code of its own (the
# shell's natural exit code is already correct there); INT/TERM must stop
# the script, not just tidy up after it — 128+signal, per convention (130
# for INT/SIGINT, 143 for TERM/SIGTERM).
# `trap - EXIT INT TERM` first, in the same handler: without it, `exit
# 130`/`exit 143` below would still trigger the EXIT trap on the way out
# and run `cleanup` a second time — harmless in itself (cleanup is
# idempotent), but it duplicates the `docker rm -f`/journal noise the
# EXIT trap is supposed to be the *only* source of on a normal exit.
trap cleanup EXIT
trap 'trap - EXIT INT TERM; cleanup; exit 130' INT
trap 'trap - EXIT INT TERM; cleanup; exit 143' TERM

# `set -e` alone does NOT abort a `VAR="$(cmd)"` assignment before this
# `if` gets a chance to run its own error message (QA-18) — the explicit
# `if ! LATEST=...` form checks the exit code directly instead of relying
# on -e to short-circuit the whole script with no explanation.
if ! LATEST="$(docker compose -f "$COMPOSE_FILE" --profile backup run --rm -T backup python -m finance_app.ops.backup latest)"; then
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
# exposure class as finding 2/QA-16. Staged in $SCRATCH_STAGING_DIR (the
# unit's RuntimeDirectory=, or a manual mktemp -d — see above), never a
# bare /tmp file, and the *directory* is bind-mounted, not the file
# directly (ADR-016 D7) — the Docker daemon resolves that mount source
# in the host mount namespace, which RuntimeDirectory=/run/<name> is
# part of and a PrivateTmp=true unit's /tmp is not.
SCRATCH_PASSWORD_FILE="$SCRATCH_STAGING_DIR/scratch_password"
printf '%s' "$SCRATCH_PASSWORD" > "$SCRATCH_PASSWORD_FILE"
chmod 600 "$SCRATCH_PASSWORD_FILE"

docker run -d --name "$SCRATCH_CONTAINER" \
    --network "$NETWORK" \
    -e POSTGRES_USER=finance_migrator \
    -e POSTGRES_PASSWORD_FILE=/run/scratch/scratch_password \
    -e POSTGRES_DB=finance_restore_drill \
    -v "$SCRATCH_STAGING_DIR:/run/scratch:ro" \
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

# Only now, with the scratch instance confirmed ready (so its entrypoint
# has already read POSTGRES_PASSWORD_FILE) — not immediately after `docker
# run -d`. The bind mount source is the *directory*, not the file (D7
# above), so a host-side unlink is visible inside the container the
# instant it happens; deleting it right after `docker run -d` races the
# container's own startup (which returns before the entrypoint has
# necessarily opened the file) and can make the entrypoint fail with
# "no such file", reproducing the exact "scratch instance never became
# ready" failure this staging change was meant to fix.
rm -f "$SCRATCH_PASSWORD_FILE"

TARGET_URL="postgresql+psycopg://finance_migrator:${SCRATCH_PASSWORD}@${SCRATCH_CONTAINER}:5432/finance_restore_drill"

# TARGET_URL (which embeds the scratch password) is passed via --target-url
# below. That's still a CLI argument, but to `docker compose run` inside a
# throwaway restore-drill container talking to a throwaway scratch
# database that's destroyed at the end of this script either way — a
# materially smaller exposure than a production credential, and
# `finance_app.ops.backup`'s own pg_dump/pg_restore calls (the ones that
# matter — finding 2) no longer put any password on argv regardless of
# how this target URL was obtained.
docker compose -f "$COMPOSE_FILE" --profile backup run --rm -T backup \
    python -m finance_app.ops.backup restore "$BACKUP_PATH" --target-url "$TARGET_URL" \
        --backup-run-id "$BACKUP_RUN_ID"

docker compose -f "$COMPOSE_FILE" --profile backup run --rm -T backup \
    python -m finance_app.ops.backup verify "$BACKUP_RUN_ID" --target-url "$TARGET_URL"
