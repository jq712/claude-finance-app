"""Deterministic writes to `user.*` — the interpretation layer the runtime
agent's write tools compose on top of raw Plaid facts (handoff §1). Every
function here is scoped to `user.*` only; none can reach `plaid.*`.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_app.db.models.user import (
    Preference,
    TransactionCategoryOverride,
    TransactionNote,
    TransactionTag,
)


def set_category_override(
    session: Session, *, transaction_id: int, category: str, source: str
) -> TransactionCategoryOverride:
    """Insert or replace the one active override for `transaction_id`."""
    stmt = (
        insert(TransactionCategoryOverride)
        .values(transaction_id=transaction_id, category=category, source=source)
        .on_conflict_do_update(
            index_elements=["transaction_id"],
            set_={"category": category, "source": source},
        )
        .returning(TransactionCategoryOverride)
    )
    return session.execute(stmt).scalar_one()


def clear_category_override(session: Session, *, transaction_id: int) -> bool:
    """Delete the override, if any. Returns whether a row was removed."""
    override = session.execute(
        select(TransactionCategoryOverride).where(
            TransactionCategoryOverride.transaction_id == transaction_id
        )
    ).scalar_one_or_none()
    if override is None:
        return False
    session.delete(override)
    return True


def set_note(session: Session, *, transaction_id: int, note: str) -> TransactionNote:
    stmt = (
        insert(TransactionNote)
        .values(transaction_id=transaction_id, note=note)
        .on_conflict_do_update(index_elements=["transaction_id"], set_={"note": note})
        .returning(TransactionNote)
    )
    return session.execute(stmt).scalar_one()


def add_tag(session: Session, *, transaction_id: int, tag: str) -> TransactionTag:
    stmt = (
        insert(TransactionTag)
        .values(transaction_id=transaction_id, tag=tag)
        .on_conflict_do_nothing(index_elements=["transaction_id", "tag"])
        .returning(TransactionTag)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is not None:
        return row
    return session.execute(
        select(TransactionTag).where(
            TransactionTag.transaction_id == transaction_id, TransactionTag.tag == tag
        )
    ).scalar_one()


def remove_tag(session: Session, *, transaction_id: int, tag: str) -> bool:
    row = session.execute(
        select(TransactionTag).where(
            TransactionTag.transaction_id == transaction_id, TransactionTag.tag == tag
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    return True


def set_preference(session: Session, *, key: str, value: dict) -> Preference:
    stmt = (
        insert(Preference)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        .returning(Preference)
    )
    return session.execute(stmt).scalar_one()
