"""Deterministic writes to `plaid.transactions`.

This is the only code path permitted to write raw Plaid facts (handoff
§4.1). The Milestone 2 sync loop calls `upsert` for `added`/`modified`
pages and `mark_removed` for `removed` pages; it never issues a hard
DELETE, so provenance is never destroyed.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_app.db.models.plaid import Transaction


@dataclass(frozen=True, slots=True)
class TransactionFields:
    """The subset of a Plaid transaction record this application persists."""

    plaid_transaction_id: str
    account_id: int
    amount: Decimal
    date: datetime.date
    name: str
    iso_currency_code: str | None = None
    authorized_date: datetime.date | None = None
    merchant_name: str | None = None
    pending: bool = False
    payment_channel: str | None = None
    plaid_category: list | None = None
    personal_finance_category: dict | None = None


def upsert(session: Session, fields: TransactionFields) -> Transaction:
    """Insert a transaction, or update it in place if the same
    `plaid_transaction_id` already exists — Plaid's sync API resends
    `modified` records under the same id, and replays of `added` pages
    must not create duplicates.
    """
    stmt = (
        insert(Transaction)
        .values(
            plaid_transaction_id=fields.plaid_transaction_id,
            account_id=fields.account_id,
            amount=fields.amount,
            iso_currency_code=fields.iso_currency_code,
            date=fields.date,
            authorized_date=fields.authorized_date,
            name=fields.name,
            merchant_name=fields.merchant_name,
            pending=fields.pending,
            payment_channel=fields.payment_channel,
            plaid_category=fields.plaid_category,
            personal_finance_category=fields.personal_finance_category,
            removed_at=None,
        )
        .on_conflict_do_update(
            index_elements=["plaid_transaction_id"],
            set_={
                "amount": fields.amount,
                "iso_currency_code": fields.iso_currency_code,
                "date": fields.date,
                "authorized_date": fields.authorized_date,
                "name": fields.name,
                "merchant_name": fields.merchant_name,
                "pending": fields.pending,
                "payment_channel": fields.payment_channel,
                "plaid_category": fields.plaid_category,
                "personal_finance_category": fields.personal_finance_category,
                "removed_at": None,
            },
        )
        .returning(Transaction)
    )
    return session.execute(stmt).scalar_one()


def mark_removed(session: Session, plaid_transaction_id: str) -> None:
    """Tombstone a transaction Plaid reports as removed. Never a hard
    DELETE — see module docstring."""
    txn = session.execute(
        select(Transaction).where(Transaction.plaid_transaction_id == plaid_transaction_id)
    ).scalar_one_or_none()
    if txn is not None:
        txn.removed_at = datetime.datetime.now(datetime.UTC)


def get_by_plaid_id(session: Session, plaid_transaction_id: str) -> Transaction | None:
    return session.execute(
        select(Transaction).where(Transaction.plaid_transaction_id == plaid_transaction_id)
    ).scalar_one_or_none()
