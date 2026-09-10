#!/bin/sh
# Exports production secrets from systemd `LoadCredentialEncrypted=` files
# (handoff §4.4, ADR-010) as the environment variables deploy/compose.yaml
# expects, then execs its arguments. Every finance-*.service unit in
# deploy/systemd/ uses this as its ExecStart/ExecStop wrapper, so the
# credential-name-to-env-var mapping lives in exactly one place instead of
# being repeated per unit.
#
# Relies on $CREDENTIALS_DIRECTORY, which systemd sets automatically for
# any unit using LoadCredential=/LoadCredentialEncrypted= (see
# systemd.exec(5)) — so this script only ever runs correctly *inside* one
# of those units. Run any other way (an interactive shell, a cron job, a
# plain SSH session) it used to silently exec its arguments with nothing
# exported, which meant `deploy/compose.yaml`'s `${VAR:?required}`
# interpolation failed with a confusing "variable is required" error
# instead of the real problem: this wrapper wasn't run under systemd at
# all. It now refuses to proceed instead — see the error message below
# for the correct invocation.
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
    echo "    -- /opt/finance-app/deploy/scripts/with-production-env.sh \\" >&2
    echo "       /opt/finance-app/deploy/scripts/finops.sh deploy <sha>" >&2
    echo "" >&2
    echo "See docs/runbooks/deploy.md for the full, copy-pasteable command per" >&2
    echo "operation (deploy/rollback/restart). Refusing to exec '$*' with an" >&2
    echo "empty credential set rather than silently proceeding without them." >&2
    exit 1
fi

export_credential() {
    name="$1"
    env_var="$2"
    file="${CREDENTIALS_DIRECTORY:-}/$name"
    if [ -n "${CREDENTIALS_DIRECTORY:-}" ] && [ -f "$file" ]; then
        value=$(cat "$file")
        export "$env_var=$value"
    fi
}

export_credential plaid_client_id PLAID_CLIENT_ID
export_credential plaid_secret PLAID_SECRET
export_credential plaid_access_token PLAID_ACCESS_TOKEN
export_credential plaid_webhook_secret PLAID_WEBHOOK_SECRET
export_credential openai_api_key OPENAI_API_KEY
export_credential anthropic_api_key ANTHROPIC_API_KEY
export_credential finance_migrator_db_password FINANCE_MIGRATOR_DB_PASSWORD
export_credential finance_app_db_password FINANCE_APP_DB_PASSWORD
export_credential finance_agent_db_password FINANCE_AGENT_DB_PASSWORD
export_credential finance_observer_db_password FINANCE_OBSERVER_DB_PASSWORD
export_credential finance_backup_db_password FINANCE_BACKUP_DB_PASSWORD
export_credential backup_encryption_key BACKUP_ENCRYPTION_KEY

exec "$@"
