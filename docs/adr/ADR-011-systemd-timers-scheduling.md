# ADR-011: systemd timers for scheduled single-host jobs

**Status:** Accepted. Unaffected by ADR-019 (2026-09-13, no Docker) in principle — systemd timers were always the scheduling mechanism regardless of packaging. Only each unit's `ExecStart=` changes, from `docker compose run ...` to a direct virtualenv-binary invocation under `/opt/finance/current`.

## Context

The system needs a daily sync, scheduled backups, and periodic health/reconciliation checks. Options were an in-application scheduler, cron, a task queue, or systemd timers.

## Decision

systemd timers, one service/timer pair per job: `finance-sync`, `finance-backup`, `finance-health`. No in-application scheduler, no Celery, no Redis.

## Consequences

- Scheduling survives application crashes and restarts — a hung process cannot silently stop the daily sync.
- Logs land in journald with the rest of the system's operational output.
- Timer units are versioned in `deploy/systemd/` and deployed with the release.
- Overlapping runs are prevented by systemd semantics plus a PostgreSQL advisory lock; no queue is needed.

## Revisit when

Scheduling needs outgrow one host. For a single-user application, they will not.
