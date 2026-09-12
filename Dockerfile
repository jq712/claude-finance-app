# Multi-stage, uv-based build (handoff §17, §29 Milestone 7; ADR-008).
#
# Produces one immutable image that serves both entrypoints — `finance`
# (the deterministic CLI / conversational agent) and `finops` (the
# production operations interface) — plus `deploy/scripts/backup.sh`'s
# `pg_dump`/`pg_restore`/`gpg` dependencies and the `docker`/`docker
# compose` CLI `finops deploy`/`rollback`/`restart` need (see the
# `deploy` service in deploy/compose.yaml), since these all run inside
# this same image rather than needing separate ones. Tagged by Git SHA in
# CI (.github/workflows/ci.yml), never `latest` alone (ADR-008).

# ---------------------------------------------------------------------------
# Stage 1: builder — resolve and install dependencies with uv, compiled
# extras included. Not present in the final image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so the layer cache survives source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY README.md ./
RUN uv sync --locked --no-dev

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal final image. No compiler, no uv, no build
# cache; a non-root user. Three package groups beyond the Python app
# itself, each earning its place for a specific finops/backup need:
#
#   postgresql-client-17  pg_dump/pg_restore, pinned to major version 17
#                          to match deploy/compose.yaml's postgres:17 —
#                          client/server version skew is not reliably
#                          supported in the newer-server direction
#                          (docs/backups.md)
#   gnupg                  backup encryption (ADR-015)
#   docker-ce-cli +         `finops deploy`/`rollback`/`restart` drive
#   docker-compose-plugin   `docker compose` themselves (docs/deployment.md).
#                           Client only — no daemon, no containerd — and
#                           only useful with a socket mounted in, which
#                           only the narrowly-scoped `deploy` Compose
#                           service does (see that service's comment in
#                           compose.yaml). The long-running `app` service
#                           never gets a socket mount.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# QA-37: baked at build time from CI's own Git SHA (`docker/build-push-
# action@v6`'s `build-args: RELEASE_ID=${{ github.sha }}` in
# publish-image, .github/workflows/ci.yml) — never overridden by
# `deploy/compose.yaml`'s `RELEASE_ID: ${RELEASE_ID:-}`, which is a
# *different*, runtime-only environment variable `probe_release` injects
# into the container it starts. `Settings.image_release_id` reads this
# one; `finance selfcheck` reports both, and `probe_release`'s
# `wrong_image` check compares this one against the release id it
# requested — the value the *image itself* carries, not an echo of
# whatever `probe_release` happened to inject, which is what made that
# check a tautology before this existed.
ARG RELEASE_ID=unknown
LABEL org.opencontainers.image.revision=$RELEASE_ID
ENV IMAGE_RELEASE_ID=$RELEASE_ID

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
        https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client-17 \
        docker-ce-cli \
        docker-compose-plugin \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 finance \
    && useradd --uid 1000 --gid finance --create-home --shell /usr/sbin/nologin finance

# `deploy/compose.yaml`'s `backup` service mounts the `finance-backups`
# named volume here. Docker initializes a *named* volume's first use from
# whatever the image already has at that path — owner, group, and mode
# included — so pre-creating it owned by the `finance` user (uid 1000,
# the user every service in compose.yaml runs as, `deploy` excepted) here
# is what makes `pg_dump --file=...`/the encrypted artifact actually
# writable at runtime. Without this, Docker creates the volume root:root
# 0755 and every backup run fails at the first write (QA-15).
RUN install -d -o finance -g finance -m 0700 /var/lib/finance-app/backups

WORKDIR /app
COPY --from=builder --chown=finance:finance /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER finance

# No default CMD: `deploy/compose.yaml` and `deploy/systemd/*.service`
# each specify the entrypoint they need (`finance ...`, `finops ...`,
# `alembic upgrade head`, `python -m finance_app.ops.backup ...`) — this
# one image serves the app process, the sync/backup/health timers, and
# migrations, so a single baked-in CMD would be wrong for most of them.
ENTRYPOINT []
CMD ["finance", "--help"]
