#!/bin/sh
# Host-run wrapper for `finops deploy`/`rollback`/`restart` (handoff §10,
# docs/deployment.md, docs/runbooks/deploy.md). These three commands need
# to drive `docker compose` themselves, so they run inside the narrowly-
# scoped `deploy` Compose service (Docker socket mounted) rather than the
# long-running `app` service, which never gets socket access — see the
# `deploy` service's comment in compose.yaml.
#
# Usage: finops.sh deploy <sha> | finops.sh rollback | finops.sh restart
#
# Run this through with-production-env.sh's `deploy` job (ADR-016 D1 —
# every `${VAR:?required}` in compose.yaml became `${VAR:-}`; the wrapper
# is what actually enforces the `deploy` service's three-credential
# requirement now: FINANCE_MIGRATOR_DB_PASSWORD, FINANCE_APP_DB_PASSWORD,
# FINANCE_OBSERVER_DB_PASSWORD):
#
#   with-production-env.sh deploy -- finops.sh deploy <sha>
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"

exec docker compose -f "$COMPOSE_FILE" --profile deploy run --rm deploy finops "$@"
