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
# systemd.exec(5)). Run outside such a unit (e.g. by hand for debugging),
# this script simply execs its arguments with nothing exported — every
# `export_credential` call below is a no-op when the directory or the
# specific credential file isn't present, so partial credential sets
# (e.g. finance-sync.service doesn't load backup_encryption_key) are
# expected, not an error.
#
# Never echoes a credential value. Never writes one to a file other than
# exporting it into this process's environment, which only this process
# and its exec'd child inherit.
set -eu

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
