"""User annotations and overrides — interpretation layered on Plaid facts.

Never modifies `plaid.*`. `finance_agent` may read and write everything in
this schema; that is exactly the write surface handoff §4.1 intends for it.
"""

import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_app.db.models import Base


class TransactionCategoryOverride(Base):
    """The user/agent-assigned category for a transaction, if any.

    One active override per transaction. Deleting the row restores the
    Plaid-reported category exactly, because the fact was never touched.
    """

    __tablename__ = "transaction_category_overrides"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_transaction_category_overrides_transaction_id"),
        {"schema": "user"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("plaid.transactions.id"))
    category: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransactionTag(Base):
    __tablename__ = "transaction_tags"
    __table_args__ = (
        UniqueConstraint("transaction_id", "tag", name="uq_transaction_tags_transaction_id_tag"),
        {"schema": "user"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("plaid.transactions.id"))
    tag: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TransactionNote(Base):
    __tablename__ = "transaction_notes"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_transaction_notes_transaction_id"),
        {"schema": "user"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("plaid.transactions.id"))
    note: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Preference(Base):
    """Durable user preferences, e.g. savings goal, preferred categories."""

    __tablename__ = "preferences"
    __table_args__ = {"schema": "user"}

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
