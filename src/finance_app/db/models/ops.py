"""Operational state — sync runs, job runs, sanitized errors. Read by
`finance_observer` for `finops` commands; never holds financial payloads."""

import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_app.db.models import Base


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="running", server_default="running")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String)


class SyncRun(Base):
    """One `/transactions/sync` run. `item_id` references `plaid.items` —
    see the Postgres FK-checking note in agent.py: referencing role does
    not need SELECT on `plaid.items` for this constraint to be enforced."""

    __tablename__ = "sync_runs"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid.items.id"))
    run_type: Mapped[str] = mapped_column(String(16))  # "scheduled" | "webhook" | "manual"
    status: Mapped[str] = mapped_column(String(32), default="running", server_default="running")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    added_count: Mapped[int] = mapped_column(default=0, server_default="0")
    modified_count: Mapped[int] = mapped_column(default=0, server_default="0")
    removed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    last_request_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String)


class OperationalError(Base):
    """Sanitized exception record — class and context only, never a raw
    payload or secret. See docs/security-model.md invariant 6."""

    __tablename__ = "errors"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    category: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(2000))
    context: Mapped[dict | None] = mapped_column(JSONB)


class BackupRun(Base):
    """One backup or restore-verification attempt (handoff §22, ADR-015).

    "A backup that has never been restored is not verified" — a
    `restore_verification` row with `verifies_backup_id` set and
    `status == "success"` is the only thing that makes a given backup
    trustworthy. `finops backup-status` reads this table."""

    __tablename__ = "backup_runs"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    run_type: Mapped[str] = mapped_column(String(32))  # "backup" | "restore_verification"
    status: Mapped[str] = mapped_column(String(32), default="running", server_default="running")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_path: Mapped[str | None] = mapped_column(String(1024))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    source_row_counts: Mapped[dict | None] = mapped_column(JSONB)
    verifies_backup_id: Mapped[int | None] = mapped_column(ForeignKey("ops.backup_runs.id"))
    verification_details: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(2000))


class Release(Base):
    """One production deploy attempt (handoff §19, ADR-008).

    `finops version` reports the `current` row; `finops rollback` promotes
    the `previous` row back to `current` and marks the failed one
    `rolled_back` — no rebuild, no registry fetch beyond what's already
    local, per ADR-008."""

    __tablename__ = "releases"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[str] = mapped_column(String(64))  # immutable Git SHA
    image_ref: Mapped[str] = mapped_column(String(512))
    # "pending" | "current" | "previous" | "history" | "failed" | "rolled_back"
    status: Mapped[str] = mapped_column(String(32))
    deployed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    health_check_status: Mapped[str | None] = mapped_column(String(32))
    rolled_back_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(String(2000))
    # The release_id that was `current` at the moment this deploy attempt
    # started (captured by `start_deploy`, QA-2) — lets a failed deploy's
    # auto-rollback target the release it actually replaced instead of
    # inferring it after the fact from generic status bookkeeping.
    replaces_release_id: Mapped[str | None] = mapped_column(String(64))
