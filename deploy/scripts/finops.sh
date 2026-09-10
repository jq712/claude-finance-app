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
# Run this through with-production-env.sh so the credentials the `deploy`
# service's compose.yaml interpolation requires (FINANCE_APP_DB_PASSWORD,
# FINANCE_OBSERVER_DB_PASSWORD — and, because Compose interpolates the
# whole file regardless of which service is selected, every other
# `${VAR:?required}` in compose.yaml too) are present:
#
#   with-production-env.sh finops.sh deploy <sha>
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$0")/.." && pwd)/compose.yaml}"

exec docker compose -f "$COMPOSE_FILE" --profile deploy run --rm deploy finops "$@"
