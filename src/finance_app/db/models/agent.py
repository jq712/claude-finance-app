"""Conversational-agent audit and context. Every tool call the runtime
agent makes is recorded here, per handoff §8/§23 — auditability without
duplicating raw transaction payloads."""

import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_app.db.models import Base


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = {"schema": "agent"}

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(String(2000))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = {"schema": "agent"}

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent.conversations.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = {"schema": "agent"}

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("agent.conversations.id"))
    question: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(32), default="running", server_default="running")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    """Audit record for every semantic tool the runtime agent invokes.

    `arguments`/`result_summary` hold structured, minimized data — never a
    raw transaction dump. This is the record that makes agent behavior
    explainable after the fact and provable in evals.
    """

    __tablename__ = "tool_calls"
    __table_args__ = {"schema": "agent"}

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent.analysis_runs.id"))
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("agent.conversations.id"))
    tool_name: Mapped[str] = mapped_column(String(128))
    arguments: Mapped[dict] = mapped_column(JSONB)
    result_summary: Mapped[dict | None] = mapped_column(JSONB)
    is_write: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Which AgentProvider adapter served this call ("openai" | "anthropic"),
    # per ADR-014 — keeps the audit trail legible across a provider switch
    # and lets Milestone 6 evals filter/compare per provider.
    provider: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CategorySuggestion(Base):
    __tablename__ = "category_suggestions"
    __table_args__ = {"schema": "agent"}

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("plaid.transactions.id"))
    suggested_category: Mapped[str] = mapped_column(String(128))
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
