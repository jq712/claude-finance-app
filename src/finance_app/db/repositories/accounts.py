"""Deterministic writes to `plaid.accounts`. Written only by the Milestone 2
sync loop (plaid/sync.py); never by the runtime agent."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_app.db.models.plaid import Account


def upsert_account(
    session: Session,
    *,
    item_id: int,
    plaid_account_id: str,
    name: str,
    official_name: str | None,
    type: str,
    subtype: str | None,
    mask: str | None,
    iso_currency_code: str | None,
) -> Account:
    """Insert the account, or update its mutable fields in place. Keyed on
    `plaid_account_id`, which Plaid guarantees stable for the account's
    lifetime."""
    stmt = (
        insert(Account)
        .values(
            item_id=item_id,
            plaid_account_id=plaid_account_id,
            name=name,
            official_name=official_name,
            type=type,
            subtype=subtype,
            mask=mask,
            iso_currency_code=iso_currency_code,
        )
        .on_conflict_do_update(
            index_elements=["plaid_account_id"],
            set_={
                "name": name,
                "official_name": official_name,
                "type": type,
                "subtype": subtype,
                "mask": mask,
                "iso_currency_code": iso_currency_code,
            },
        )
        .returning(Account)
    )
    return session.execute(stmt).scalar_one()


def get_by_plaid_account_id(session: Session, plaid_account_id: str) -> Account | None:
    return session.execute(
        select(Account).where(Account.plaid_account_id == plaid_account_id)
    ).scalar_one_or_none()
