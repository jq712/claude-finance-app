"""Raw Plaid source-of-truth tables.

Written only by deterministic ingestion/reconciliation code (see
db/repositories/transactions.py and the Milestone 2 sync loop). The
`finance_agent` database role has SELECT only here — enforced by grants in
migrations/versions, proven by tests/security/test_role_grants.py.

Transactions are never hard-deleted: a Plaid "removed" event sets
`removed_at` instead, so provenance is never destroyed.
"""

import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_app.db.models import Base


class Item(Base):
    """A Plaid Item. This application connects exactly one."""

    __tablename__ = "items"
    __table_args__ = {"schema": "plaid"}

    id: Mapped[int] = mapped_column(primary_key=True)
    plaid_item_id: Mapped[str] = mapped_column(String(64), unique=True)
    institution_id: Mapped[str | None] = mapped_column(String(64))
    institution_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    accounts: Mapped[list["Account"]] = relationship(back_populates="item")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = {"schema": "plaid"}

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid.items.id"))
    plaid_account_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    official_name: Mapped[str | None] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))
    subtype: Mapped[str | None] = mapped_column(String(32))
    mask: Mapped[str | None] = mapped_column(String(8))
    iso_currency_code: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    item: Mapped[Item] = relationship(back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account")


class Transaction(Base):
    """A single Plaid transaction record.

    `amount` follows Plaid's sign convention: positive is money leaving the
    account (a debit/spend), negative is money entering it (a credit/refund).
    Analytics code, not this model, decides how to interpret that.
    """

    __tablename__ = "transactions"
    __table_args__ = {"schema": "plaid"}

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("plaid.accounts.id"))
    plaid_transaction_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    iso_currency_code: Mapped[str | None] = mapped_column(String(3))
    date: Mapped[datetime.date] = mapped_column(Date)
    authorized_date: Mapped[datetime.date | None] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(512))
    merchant_name: Mapped[str | None] = mapped_column(String(255))
    pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    payment_channel: Mapped[str | None] = mapped_column(String(32))
    plaid_category: Mapped[list | None] = mapped_column(JSONB)
    personal_finance_category: Mapped[dict | None] = mapped_column(JSONB)
    removed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    account: Mapped[Account] = relationship(back_populates="transactions")


class SyncState(Base):
    """Durable cursor/status for the single Plaid Item's Transactions Sync."""

    __tablename__ = "sync_state"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_sync_state_item_id"),
        {"schema": "plaid"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid.items.id"))
    cursor: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(32), default="idle", server_default="idle")
    last_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
    last_request_id: Mapped[str | None] = mapped_column(String(64))
    added_count: Mapped[int | None] = mapped_column()
    modified_count: Mapped[int | None] = mapped_column()
    removed_count: Mapped[int | None] = mapped_column()
