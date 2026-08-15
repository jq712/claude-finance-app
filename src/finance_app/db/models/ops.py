"""Operational state — sync runs, job runs, sanitized errors. Read by
`finance_observer` for `finops` commands; never holds financial payloads."""

import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
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
