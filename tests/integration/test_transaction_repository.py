import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.db.repositories.transactions import (
    TransactionFields,
    get_by_plaid_id,
    mark_removed,
    upsert,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def app_session(role_engine):
    engine = role_engine("finance_app")
    with Session(engine) as session:
        # Isolate each test's rows behind a savepoint-free transaction
        # rollback — simplest correct thing for a single-connection test.
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('test-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'test-account', 'Test Checking', 'depository') RETURNING id"
            ),
            {"item_id": item_id},
        ).scalar_one()
        session.commit()
        try:
            yield session, account_id
        finally:
            session.rollback()
            session.execute(
                text("DELETE FROM plaid.transactions WHERE account_id = :a"), {"a": account_id}
            )
            session.execute(text("DELETE FROM plaid.accounts WHERE id = :a"), {"a": account_id})
            session.execute(text("DELETE FROM plaid.items WHERE id = :i"), {"i": item_id})
            session.commit()


def _fields(account_id: int, **overrides) -> TransactionFields:
    base = {
        "plaid_transaction_id": "synthetic-txn-1",
        "account_id": account_id,
        "amount": Decimal("42.10"),
        "date": datetime.date(2026, 1, 5),
        "name": "MOONLIGHT DINER",
    }
    base.update(overrides)
    return TransactionFields(**base)


def test_upsert_inserts_a_new_transaction(app_session) -> None:
    session, account_id = app_session
    txn = upsert(session, _fields(account_id))
    session.commit()

    assert txn.plaid_transaction_id == "synthetic-txn-1"
    assert txn.amount == Decimal("42.10")
    assert txn.removed_at is None


def test_upsert_is_idempotent_on_plaid_transaction_id(app_session) -> None:
    session, account_id = app_session
    upsert(session, _fields(account_id, amount=Decimal("42.10")))
    session.commit()

    # Plaid resent this as a "modified" record with a new amount and a
    # pending flip — same plaid_transaction_id must update in place.
    upsert(session, _fields(account_id, amount=Decimal("45.00"), pending=True))
    session.commit()

    txn = get_by_plaid_id(session, "synthetic-txn-1")
    assert txn is not None
    assert txn.amount == Decimal("45.00")
    assert txn.pending is True


def test_mark_removed_tombstones_instead_of_deleting(app_session) -> None:
    session, account_id = app_session
    upsert(session, _fields(account_id))
    session.commit()

    mark_removed(session, "synthetic-txn-1")
    session.commit()

    txn = get_by_plaid_id(session, "synthetic-txn-1")
    assert txn is not None, "removed transaction must still exist — provenance is never destroyed"
    assert txn.removed_at is not None
