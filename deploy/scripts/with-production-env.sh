#!/bin/sh
# Exports production secrets from systemd `LoadCredentialEncrypted=` files
# (handoff §4.4, ADR-010) as the environment variables deploy/compose.yaml
# expects, then execs its arguments. Every finance-*.service unit in
# deploy/systemd/ uses this as its ExecStart/ExecStop wrapper, so the
# credential-name-to-env-var mapping lives in exactly one place instead of
# being repeated per unit.
#
# ADR-016 D1: `deploy/compose.yaml` no longer enforces credential
# requirements itself (every `${VAR:?required}` became `${VAR:-}`) —
# Compose interpolates the *entire* file before running any one service,
# regardless of `--profile`, so a whole-file assertion could never express
# a per-job requirement. This script is now where that enforcement lives:
#
#     with-production-env.sh <job> -- <command...>
#     job ∈ { app | migrate | sync | backup | finops | deploy | health |
#              restore-drill | postgres }
#
# `postgres` is one job beyond ADR-016 D1's literal eight-entry table: it
# is what `finance-app.service`'s bare `docker compose up -d`/`down`
# (no `--profile`, unlike every other unit) actually needs — under D2
# that brings up only the un-profiled `postgres` service, which needs
# only `FINANCE_MIGRATOR_DB_PASSWORD` (its own `POSTGRES_PASSWORD`). Kept
# as its own named job rather than reusing `migrate` (which happens to
# require the identical single credential) so the job name still
# describes what actually runs, and so the drift test can assert this
# job's set independently of `migrate`'s.
#
# For the named job, this script exports *only* that job's required
# variables (never the entire credential directory), fails loudly —
# naming both the job and the missing credential — if a required one is
# absent, and rejects an empty value for a required credential exactly as
# hard as a missing one (an empty `BACKUP_ENCRYPTION_KEY`, in particular,
# would make `gpg --symmetric` silently produce a backup anyone can
# decrypt). This mapping is the contract `tests/unit/
# test_deploy_topology_regression.py`'s drift test keeps in sync with
# `deploy/compose.yaml` — never delete that test, even after this script
# changes shape again.
#
# Relies on $CREDENTIALS_DIRECTORY, which systemd sets automatically for
# any unit using LoadCredential=/LoadCredentialEncrypted= (see
# systemd.exec(5)) — so this script only ever runs correctly *inside* one
# of those units. Run any other way (an interactive shell, a cron job, a
# plain SSH session) it refuses to proceed rather than silently exporting
# nothing — see the error message below for the correct invocation.
#
# Never echoes a credential value. Never writes one to a file other than
# exporting it into this process's environment, which only this process
# and its exec'd child inherit.
set -eu

if [ -z "${CREDENTIALS_DIRECTORY:-}" ]; then
    echo "with-production-env.sh: \$CREDENTIALS_DIRECTORY is not set." >&2
    echo "" >&2
    echo "This script only decrypts production credentials inside a systemd unit" >&2
    echo "using LoadCredentialEncrypted= (systemd.exec(5)) — a bare interactive" >&2
    echo "shell has no route to the TPM/machine-key-bound decryption systemd-creds" >&2
    echo "relies on. Run it via 'systemd-run' instead, e.g. for finops:" >&2
    echo "" >&2
    echo "  sudo systemd-run --pty --wait --collect --same-dir \\" >&2
    echo "    \$(sed -n 's/^LoadCredentialEncrypted=/--property=LoadCredentialEncrypted=/p' \\" >&2
    echo "        /etc/systemd/system/finance-app.service) \\" >&2
    echo "    -- /opt/finance-app/deploy/scripts/with-production-env.sh deploy -- \\" >&2
    echo "       /opt/finance-app/deploy/scripts/finops.sh deploy <sha>" >&2
    echo "" >&2
    echo "See docs/runbooks/deploy.md for the full, copy-pasteable command per" >&2
    echo "operation (deploy/rollback/restart). Refusing to exec '$*' with an" >&2
    echo "empty credential set rather than silently proceeding without them." >&2
    exit 1
fi

if [ "$#" -lt 1 ]; then
    echo "usage: with-production-env.sh <job> -- <command...>" >&2
    echo "  job in: app migrate sync backup finops deploy health restore-drill postgres" >&2
    exit 2
fi

JOB="$1"
shift
if [ "${1:-}" = "--" ]; then
    shift
fi

if [ "$#" -lt 1 ]; then
    echo "with-production-env.sh: no command given to exec after '<job> --'" >&2
    exit 2
fi

# Validated here, directly in this shell — not inside a function called
# via command substitution, where `exit` would only terminate the
# subshell created for that substitution and let the rest of this script
# continue with an empty (i.e. unenforced) required-credential set.
case "$JOB" in
    app | migrate | sync | backup | restore-drill | finops | deploy | health | postgres) : ;;
    *)
        echo "with-production-env.sh: unknown job '$JOB' (expected one of: app migrate sync backup finops deploy health restore-drill postgres)" >&2
        exit 2
        ;;
esac

# name=credential-file-stem, env=variable name deploy/compose.yaml expects.
_credential_names() {
    printf '%s\n' \
        "plaid_client_id=PLAID_CLIENT_ID" \
        "plaid_secret=PLAID_SECRET" \
        "plaid_access_token=PLAID_ACCESS_TOKEN" \
        "plaid_webhook_secret=PLAID_WEBHOOK_SECRET" \
        "openai_api_key=OPENAI_API_KEY" \
        "anthropic_api_key=ANTHROPIC_API_KEY" \
        "finance_migrator_db_password=FINANCE_MIGRATOR_DB_PASSWORD" \
        "finance_app_db_password=FINANCE_APP_DB_PASSWORD" \
        "finance_agent_db_password=FINANCE_AGENT_DB_PASSWORD" \
        "finance_observer_db_password=FINANCE_OBSERVER_DB_PASSWORD" \
        "finance_backup_db_password=FINANCE_BACKUP_DB_PASSWORD" \
        "backup_encryption_key=BACKUP_ENCRYPTION_KEY"
}

# The ADR-016 D1 credential matrix: job -> space-separated required
# FINANCE_*/PLAID_*/OPENAI_*/ANTHROPIC_* env var names. `app`'s "active
# provider key" requirement (OPENAI_API_KEY | ANTHROPIC_API_KEY) is
# enforced separately below, not listed here, since it is an either/or —
# the matrix format below is a plain AND of required names.
_required_for_job() {
    case "$1" in
        app) echo "FINANCE_APP_DB_PASSWORD FINANCE_AGENT_DB_PASSWORD" ;;
        migrate) echo "FINANCE_MIGRATOR_DB_PASSWORD" ;;
        sync) echo "FINANCE_APP_DB_PASSWORD PLAID_CLIENT_ID PLAID_SECRET PLAID_ACCESS_TOKEN" ;;
        backup | restore-drill)
            echo "FINANCE_BACKUP_DB_PASSWORD FINANCE_APP_DB_PASSWORD BACKUP_ENCRYPTION_KEY"
            ;;
        finops) echo "FINANCE_OBSERVER_DB_PASSWORD" ;;
        health) echo "FINANCE_APP_DB_PASSWORD" ;;
        deploy)
            echo "FINANCE_MIGRATOR_DB_PASSWORD FINANCE_APP_DB_PASSWORD FINANCE_OBSERVER_DB_PASSWORD"
            ;;
        postgres) echo "FINANCE_MIGRATOR_DB_PASSWORD" ;;
    esac
}

REQUIRED="$(_required_for_job "$JOB")"

# Only export variables belonging to this job's own credential set — even
# though $CREDENTIALS_DIRECTORY may hold every credential the unit's
# LoadCredentialEncrypted= list declares (that list is trimmed per-job in
# deploy/systemd/*.service, but this script does not rely on that trim
# alone for the boundary).
#
# POSIX `sh` cannot export a variable from inside a piped `while` (it runs
# in a subshell); stage name=value pairs through a private temp file
# instead and export them in this shell afterward. Never world-readable,
# removed unconditionally.
_WPE_TMP="$(mktemp)"
chmod 600 "$_WPE_TMP"
trap 'rm -f "$_WPE_TMP"' EXIT

_credential_names | while IFS='=' read -r name env_var; do
    case " $REQUIRED " in
        *" $env_var "*) : ;;
        *) continue ;;
    esac
    file="${CREDENTIALS_DIRECTORY}/$name"
    if [ -f "$file" ]; then
        value=$(cat "$file")
        printf '%s\n' "$env_var=$value" >> "$_WPE_TMP"
    fi
done

while IFS= read -r line; do
    export "$line"
done < "$_WPE_TMP"

# `app`'s active-provider-key requirement is either/or, not a fixed name
# in the matrix above — resolve it against AGENT_PROVIDER (defaulting to
# openai, matching config/settings.py's own default) and export/require
# whichever key is active.
if [ "$JOB" = "app" ]; then
    provider="${AGENT_PROVIDER:-openai}"
    case "$provider" in
        openai) provider_env="OPENAI_API_KEY"; provider_cred="openai_api_key" ;;
        anthropic) provider_env="ANTHROPIC_API_KEY"; provider_cred="anthropic_api_key" ;;
        *)
            echo "with-production-env.sh: job 'app' has unknown AGENT_PROVIDER '$provider'" >&2
            exit 1
            ;;
    esac
    file="${CREDENTIALS_DIRECTORY}/$provider_cred"
    if [ -f "$file" ]; then
        value=$(cat "$file")
        export "$provider_env=$value"
    fi
    REQUIRED="$REQUIRED $provider_env"
fi

# Fail loudly, naming both the job and the missing/empty credential — an
# empty value is rejected exactly as hard as a missing one (a
# `${BACKUP_ENCRYPTION_KEY:-}` that resolves to the empty string must
# never reach `gpg --symmetric` unnoticed).
for env_var in $REQUIRED; do
    eval "value=\${$env_var:-}"
    if [ -z "$value" ]; then
        echo "with-production-env.sh: job '$JOB' requires $env_var, but it is missing or empty" >&2
        echo "(checked \$CREDENTIALS_DIRECTORY/<name>.cred via systemd LoadCredentialEncrypted=)" >&2
        exit 1
    fi
done

exec "$@"
