"""Integration tests for `finance_app.analytics.transactions`, the query
layer behind `finance transactions recent`/`finance transactions search`.
Uses the same golden dataset and effective-transaction view as
tests/integration/test_analytics.py, so category labels and netting here
must agree with the standalone spending tests."""

import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.analytics.transactions import list_recent, search_transactions
from finance_app.db.repositories.transactions import TransactionFields, upsert
from tests.plaid_fixtures.synthetic import golden_month

pytestmark = pytest.mark.integration

MONTH_START = datetime.date(2026, 1, 1)


@pytest.fixture
def seeded_session(role_engine):
    """One Item/Account with `golden_month` loaded for January 2026."""
    engine = role_engine("finance_app")
    with Session(engine) as session:
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('transactions-test-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'transactions-test-account', 'Test Checking', 'depository') "
                "RETURNING id"
            ),
            {"item_id": item_id},
        ).scalar_one()
        for txn in golden_month(MONTH_START):
            upsert(
                session,
                TransactionFields(
                    plaid_transaction_id=txn.plaid_transaction_id,
                    account_id=account_id,
                    amount=txn.amount,
                    date=txn.date,
                    name=txn.name,
                    merchant_name=txn.merchant_name,
                    pending=txn.pending,
                    payment_channel=txn.payment_channel,
                    plaid_category=txn.plaid_category,
                    authorized_date=txn.authorized_date,
                ),
            )
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


def test_list_recent_returns_newest_first(seeded_session) -> None:
    session, _account_id = seeded_session
    as_of = MONTH_START + datetime.timedelta(days=31)

    records = list_recent(session, as_of=as_of, days=31, limit=100)

    dates = [r.date for r in records]
    assert dates == sorted(dates, reverse=True)
    assert any(r.name == "ACME CORP PAYROLL" for r in records)


def test_list_recent_respects_limit(seeded_session) -> None:
    session, _account_id = seeded_session
    as_of = MONTH_START + datetime.timedelta(days=31)

    records = list_recent(session, as_of=as_of, days=31, limit=2)

    assert len(records) == 2


def test_search_matches_name_case_insensitively(seeded_session) -> None:
    session, _account_id = seeded_session

    records = search_transactions(
        session,
        query="diner",
        start=MONTH_START,
        end=MONTH_START + datetime.timedelta(days=31),
    )

    names = {r.name for r in records}
    assert names == {"MOONLIGHT DINER", "MOONLIGHT DINER REFUND"}


def test_search_matches_merchant_name(seeded_session) -> None:
    session, _account_id = seeded_session

    records = search_transactions(
        session,
        query="trader joe",
        start=MONTH_START,
        end=MONTH_START + datetime.timedelta(days=31),
    )

    assert len(records) == 1
    assert records[0].merchant_name == "Trader Joe's"
    assert records[0].amount == Decimal("87.32")
    assert records[0].effective_category == "Groceries"


def test_search_returns_nothing_for_no_match(seeded_session) -> None:
    session, _account_id = seeded_session

    records = search_transactions(
        session,
        query="nonexistent merchant xyz",
        start=MONTH_START,
        end=MONTH_START + datetime.timedelta(days=31),
    )

    assert records == []
