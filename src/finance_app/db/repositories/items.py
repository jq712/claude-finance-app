"""Deterministic writes to `plaid.items` — the single Plaid Item this
application connects (handoff §2). Written only by the Milestone 2 sync
bootstrap (see plaid/sync.py); never by the runtime agent."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_app.db.models.plaid import Item


def upsert_item(
    session: Session,
    *,
    plaid_item_id: str,
    institution_id: str | None,
    institution_name: str | None,
) -> Item:
    """Insert the Item, or update institution metadata in place if it
    already exists. Keyed on `plaid_item_id`, which is stable for the
    lifetime of the Plaid connection."""
    stmt = (
        insert(Item)
        .values(
            plaid_item_id=plaid_item_id,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        .on_conflict_do_update(
            index_elements=["plaid_item_id"],
            set_={
                "institution_id": institution_id,
                "institution_name": institution_name,
            },
        )
        .returning(Item)
    )
    return session.execute(stmt).scalar_one()


def get_by_plaid_item_id(session: Session, plaid_item_id: str) -> Item | None:
    return session.execute(
        select(Item).where(Item.plaid_item_id == plaid_item_id)
    ).scalar_one_or_none()
