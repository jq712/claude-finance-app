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
# absent or empty or whitespace-only, and never trusts a value already
# present in this process's own (inherited) environment as a substitute
# for one actually read from $CREDENTIALS_DIRECTORY. This mapping is the
# contract `tests/unit/test_deploy_topology_regression.py`'s drift test
# keeps in sync with `deploy/compose.yaml` — never delete that test, even
# after this script changes shape again.
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
    echo "    --property=LoadCredentialEncrypted=finance_migrator_db_password:/etc/finance-app/credentials/finance_migrator_db_password.cred \\" >&2
    echo "    --property=LoadCredentialEncrypted=finance_app_db_password:/etc/finance-app/credentials/finance_app_db_password.cred \\" >&2
    echo "    --property=LoadCredentialEncrypted=finance_observer_db_password:/etc/finance-app/credentials/finance_observer_db_password.cred \\" >&2
    echo "    -- /opt/finance-app/deploy/scripts/with-production-env.sh deploy -- \\" >&2
    echo "       /opt/finance-app/deploy/scripts/finops.sh deploy <sha>" >&2
    echo "" >&2
    echo "(that is the 'deploy' job's own credential set — NOT derived from" >&2
    echo "finance-app.service, which under ADR-016 D1 holds only its own, much" >&2
    echo "narrower, 'postgres' job credential.)" >&2
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
            # `probe_release` also drives `docker compose --profile app run
            # --rm app finance selfcheck` *from inside this job's own
            # process* — Compose interpolates the `app` service block
            # against this environment too, so `FINANCE_AGENT_DB_PASSWORD`
            # and the active provider key resolve empty there. Same
            # documented, harmless gap as the `health` job below:
            # `finance selfcheck` never touches AGENT_DATABASE_URL or a
            # provider key (src/finance_app/ops/selfcheck.py). Do not
            # "fix" this by adding those to this set — the whole point of
            # D1 is that `deploy` never holds a provider key.
            echo "FINANCE_MIGRATOR_DB_PASSWORD FINANCE_APP_DB_PASSWORD FINANCE_OBSERVER_DB_PASSWORD"
            ;;
        postgres) echo "FINANCE_MIGRATOR_DB_PASSWORD" ;;
    esac
}

REQUIRED="$(_required_for_job "$JOB")"

# `app`'s active-provider-key requirement is either/or, not a fixed name
# in the matrix above — resolve it against AGENT_PROVIDER (defaulting to
# openai, matching config/settings.py's own default) and add whichever
# key is active to $REQUIRED before export/validation runs, so both go
# through the exact same path as every other credential below.
if [ "$JOB" = "app" ]; then
    provider="${AGENT_PROVIDER:-openai}"
    case "$provider" in
        openai) provider_env="OPENAI_API_KEY" ;;
        anthropic) provider_env="ANTHROPIC_API_KEY" ;;
        *)
            echo "with-production-env.sh: job 'app' has unknown AGENT_PROVIDER '$provider'" >&2
            exit 1
            ;;
    esac
    REQUIRED="$REQUIRED $provider_env"
fi

# Every credential-mapped variable is unset first, so what this job's
# child process inherits reflects only what this script itself loaded
# from $CREDENTIALS_DIRECTORY for *this* job — never a value already
# sitting in the environment this script was invoked with (an
# EnvironmentFile=, a stray export in an interactive shell). Without this,
# a required credential whose file is absent could still appear "present"
# by inheritance, silently defeating the missing/empty check below.
for pair in $(_credential_names); do
    unset "${pair#*=}" 2>/dev/null || true
done

# Export directly in this shell — no piped `while` (POSIX `sh` runs the
# pipeline's right-hand side in a subshell, so anything it exports is
# lost the moment the pipe closes) and no intermediate file (the previous
# approach: writing plaintext credentials to a `mktemp` file. `exec "$@"`
# below replaces this process image, so its `EXIT` trap never ran and the
# file survived on disk for as long as the parent unit stayed active —
# indefinitely for `finance-app.service`'s `RemainAfterExit=yes`. That
# file also re-parsed credential *content* as `NAME=VALUE` shell
# assignments on read-back, so a credential containing a newline could
# silently truncate itself or inject a value into an unrelated variable).
# `$(_credential_names)` is safe to word-split on default IFS: every
# entry is `stem=ENV_VAR` with no internal whitespace.
for pair in $(_credential_names); do
    name="${pair%%=*}"
    env_var="${pair#*=}"
    case " $REQUIRED " in
        *" $env_var "*) : ;;
        *) continue ;;
    esac
    file="${CREDENTIALS_DIRECTORY}/$name"
    [ -f "$file" ] || continue
    value=$(cat "$file")
    # A credential containing a newline must never be exported partially
    # or re-interpreted — reject it outright rather than silently
    # truncating it or letting it inject another variable (both possible
    # if this value were ever round-tripped through a NAME=VALUE line
    # instead of `export` taking it as one opaque string, as here).
    if [ "$(printf '%s\n' "$value" | wc -l)" -gt 1 ]; then
        echo "with-production-env.sh: credential '$name' (job '$JOB') contains an embedded newline — refusing to export it" >&2
        exit 1
    fi
    # A stray carriage return (CRLF line endings from a Windows-side
    # credential source, an editor, a paste path) is not caught by the
    # newline check above — `wc -l` still sees one line — but it silently
    # corrupts the value just the same (a `BACKUP_ENCRYPTION_KEY` ending
    # in \r would encrypt backups under a passphrase that differs from
    # whatever was recorded out-of-band, discovered only at restore time).
    case "$value" in
        *"$(printf '\r')"*)
            echo "with-production-env.sh: credential '$name' (job '$JOB') contains a carriage return — refusing to export it" >&2
            exit 1
            ;;
    esac
    export "$env_var=$value"
done

# Fail loudly, naming both the job and the missing/empty/whitespace-only
# credential — rejected exactly as hard as a missing one (a
# `${BACKUP_ENCRYPTION_KEY:-}` that resolves to whitespace must never
# reach `gpg --symmetric` unnoticed; likewise a single stray keystroke
# while minting a credential, per docs/runbooks/deploy.md).
for env_var in $REQUIRED; do
    eval "value=\${$env_var:-}"
    stripped=$(printf '%s' "$value" | tr -d '[:space:]')
    if [ -z "$stripped" ]; then
        echo "with-production-env.sh: job '$JOB' requires $env_var, but it is missing, empty, or whitespace-only" >&2
        echo "(checked \$CREDENTIALS_DIRECTORY/<name>.cred via systemd LoadCredentialEncrypted=)" >&2
        exit 1
    fi
done

exec "$@"
