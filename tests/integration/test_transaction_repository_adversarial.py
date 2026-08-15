"""Adversarial tests for `finance_app.db.repositories.transactions`.

These probe scenarios the happy-path tests in
`tests/integration/test_transaction_repository.py` do not cover, per
handoff §30's Plaid/data-ingestion and financial-correctness test lists.
Milestone 1 only builds the repository layer — Milestone 2 builds the sync
loop that calls it — so these tests exist to prove (or disprove) that the
repository layer does not foreclose correct handling of the shapes Plaid's
`/transactions/sync` API is documented to produce.
"""

import datetime
import threading
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from finance_app.db.repositories.transactions import (
    TransactionFields,
    get_by_plaid_id,
    mark_removed,
    upsert,
)

pytestmark = pytest.mark.integration


def _app_dsn() -> str:
    return "postgresql+psycopg://finance_app:devpassword@localhost:5433/finance_dev"


@pytest.fixture
def app_session(role_engine):
    engine = role_engine("finance_app")
    with Session(engine) as session:
        item_id = session.execute(
            text(
                "INSERT INTO plaid.items (plaid_item_id, institution_name) "
                "VALUES ('adv-test-item', 'Test Bank') RETURNING id"
            )
        ).scalar_one()
        account_id = session.execute(
            text(
                "INSERT INTO plaid.accounts (item_id, plaid_account_id, name, type) "
                "VALUES (:item_id, 'adv-test-account', 'Test Checking', 'depository') "
                "RETURNING id"
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
        "plaid_transaction_id": "adv-txn-1",
        "account_id": account_id,
        "amount": Decimal("42.10"),
        "date": datetime.date(2026, 1, 5),
        "name": "MOONLIGHT DINER",
    }
    base.update(overrides)
    return TransactionFields(**base)


# ---------------------------------------------------------------------------
# Out-of-order removed-before-added (Plaid sync can produce this mid-page)
# ---------------------------------------------------------------------------


def test_mark_removed_before_the_transaction_ever_existed_is_a_silent_no_op(app_session) -> None:
    """Plaid's sync API can, within the same or adjacent pages, report a
    `removed` event for an id the local database has never seen (e.g. a
    pending transaction removed before its `added` page was processed, or
    reordering across a restart). `mark_removed` currently does a
    SELECT-then-maybe-update and does nothing if no row is found — it does
    not record that this id should be considered pre-removed.

    This test proves the current (silent no-op) behavior and documents the
    consequence: if a later `upsert` for the same id then runs (e.g. a
    stale retried page, or the id being reused across a restart boundary),
    the transaction is inserted *live*, never tombstoned, even though Plaid
    told us it was removed first.
    """
    session, account_id = app_session

    # "removed" arrives first for an id we've never seen.
    mark_removed(session, "adv-txn-never-added")
    session.commit()

    assert get_by_plaid_id(session, "adv-txn-never-added") is None

    # "added" arrives afterward (out-of-order delivery / retry).
    txn = upsert(session, _fields(account_id, plaid_transaction_id="adv-txn-never-added"))
    session.commit()

    # Current behavior: the transaction ends up live, not tombstoned. If
    # Milestone 2's sync loop relies on `mark_removed` to be authoritative
    # regardless of arrival order, this is a gap — a removed-before-added
    # transaction silently resurrects as an ordinary active transaction.
    assert txn.removed_at is None, (
        "if this assertion ever starts failing, the repository has grown "
        "out-of-order removal handling and this test (and its docstring) "
        "should be updated to match; today it silently drops the removal"
    )


def test_upsert_after_mark_removed_resurrects_without_any_signal(app_session) -> None:
    """Symmetric case: a transaction is tombstoned, then a later upsert
    (e.g. a stale retried `modified` page for the same id) revives it —
    `removed_at` is unconditionally reset to NULL by `upsert`, with no way
    for a caller to distinguish "this is a legitimate re-add" from "this is
    a stale replay of a page that predates the removal"."""
    session, account_id = app_session

    upsert(session, _fields(account_id))
    session.commit()
    mark_removed(session, "adv-txn-1")
    session.commit()

    txn = get_by_plaid_id(session, "adv-txn-1")
    assert txn is not None
    assert txn.removed_at is not None

    # A stale/replayed "added" or "modified" page for the same id arrives.
    revived = upsert(session, _fields(account_id))
    session.commit()

    assert revived.removed_at is None, (
        "upsert unconditionally clears removed_at — a stale replay after a "
        "removal silently un-tombstones the row with no audit trail of the "
        "conflict"
    )


# ---------------------------------------------------------------------------
# Money correctness
# ---------------------------------------------------------------------------


def test_amount_round_trips_exact_cents(app_session) -> None:
    session, account_id = app_session
    for amount in (
        Decimal("0.01"),
        Decimal("-0.01"),
        Decimal("9999999999.99"),
        Decimal("-9999999999.99"),
        Decimal("0.00"),
    ):
        txn = upsert(
            session,
            _fields(account_id, plaid_transaction_id=f"adv-amt-{amount}", amount=amount),
        )
        session.commit()
        assert txn.amount == amount, f"{amount} round-tripped as {txn.amount}"
        # Confirm no float coercion anywhere in the path: re-fetch from a
        # fresh query and assert the type is still Decimal.
        refetched = get_by_plaid_id(session, f"adv-amt-{amount}")
        assert refetched is not None
        assert isinstance(refetched.amount, Decimal)


def test_amount_beyond_numeric_precision_is_rejected_not_silently_truncated(app_session) -> None:
    """NUMERIC(12,2) allows at most 10 integer digits. A caller that passes
    an out-of-range amount (e.g. a unit-conversion bug upstream) must get a
    loud error, not silent data loss."""
    session, account_id = app_session
    with pytest.raises((DataError, IntegrityError)):
        upsert(
            session,
            _fields(
                account_id,
                plaid_transaction_id="adv-amt-overflow",
                amount=Decimal("99999999999.99"),
            ),
        )
    session.rollback()


def test_amount_with_sub_cent_precision_is_silently_rounded_not_rejected(app_session) -> None:
    """If upstream (Milestone 2) code ever produces a Decimal with more
    than 2 decimal places — e.g. from an intermediate float division bug —
    Postgres's NUMERIC(12,2) column silently rounds it on write rather than
    raising. This test documents that the repository layer provides no
    defense-in-depth here: garbage-in is silently coerced, not rejected.
    """
    session, account_id = app_session
    txn = upsert(
        session,
        _fields(account_id, plaid_transaction_id="adv-subcent", amount=Decimal("42.105")),
    )
    session.commit()
    # Postgres numeric cast rounds half-away-from-zero: 42.105 -> 42.11.
    assert txn.amount == Decimal("42.11"), (
        "sub-cent precision is silently rounded to the cent by the "
        "database, not rejected by the repository layer — if Milestone 2 "
        "ever hands upsert() a Decimal derived from float arithmetic, this "
        "is the failure mode: wrong-by-a-cent, no error"
    )


def test_negative_zero_amount_normalizes_to_positive_zero(app_session) -> None:
    session, account_id = app_session
    txn = upsert(
        session,
        _fields(account_id, plaid_transaction_id="adv-negzero", amount=Decimal("-0.00")),
    )
    session.commit()
    assert txn.amount == Decimal("0.00")
    assert not txn.amount.is_signed(), "negative zero should not persist as a signed zero"


# ---------------------------------------------------------------------------
# FK / constraint behavior
# ---------------------------------------------------------------------------


def test_upsert_with_nonexistent_account_id_raises_integrity_error(app_session) -> None:
    session, _account_id = app_session
    with pytest.raises(IntegrityError):
        upsert(session, _fields(account_id=999_999_999, plaid_transaction_id="adv-bad-fk"))
    session.rollback()

    # And the failed insert must not have left a half-written row visible
    # to a subsequent lookup in a fresh transaction.
    assert get_by_plaid_id(session, "adv-bad-fk") is None


def test_duplicate_plaid_transaction_id_via_raw_insert_is_rejected(app_session) -> None:
    """Guards the unique constraint the repository's ON CONFLICT logic
    depends on: a raw duplicate INSERT (bypassing `upsert`, as a buggy
    caller might) must fail loudly, not silently create a second row."""
    session, account_id = app_session
    upsert(session, _fields(account_id))
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO plaid.transactions "
                "(account_id, plaid_transaction_id, amount, date, name) "
                "VALUES (:a, 'adv-txn-1', 1.00, now(), 'duplicate')"
            ),
            {"a": account_id},
        )
    session.rollback()

    count = session.execute(
        text("SELECT count(*) FROM plaid.transactions WHERE plaid_transaction_id = 'adv-txn-1'")
    ).scalar_one()
    assert count == 1


def test_null_plaid_transaction_id_is_rejected_at_the_database(app_session) -> None:
    session, account_id = app_session
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO plaid.transactions "
                "(account_id, plaid_transaction_id, amount, date, name) "
                "VALUES (:a, NULL, 1.00, now(), 'no id')"
            ),
            {"a": account_id},
        )
    session.rollback()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_upserts_of_the_same_plaid_transaction_id_do_not_duplicate(
    app_session,
) -> None:
    """Simulates two overlapping sync runs (or a retried page racing the
    original) both upserting the same plaid_transaction_id at once. The
    unique constraint + ON CONFLICT DO UPDATE should serialize these at the
    database level with no duplicate row and no unhandled exception."""
    session, account_id = app_session
    session.commit()  # ensure account/item are visible to other connections

    errors: list[BaseException] = []
    results: list[Decimal] = []
    barrier = threading.Barrier(2)

    def worker(amount: Decimal) -> None:
        engine = create_engine(_app_dsn())
        try:
            with Session(engine) as s:
                barrier.wait(timeout=5)
                txn = upsert(
                    s,
                    _fields(
                        account_id,
                        plaid_transaction_id="adv-concurrent-txn",
                        amount=amount,
                    ),
                )
                s.commit()
                results.append(txn.amount)
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [
        threading.Thread(target=worker, args=(Decimal("10.00"),)),
        threading.Thread(target=worker, args=(Decimal("20.00"),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent upsert raised: {errors}"

    fresh_engine = create_engine(_app_dsn())
    with Session(fresh_engine) as verify:
        count = verify.execute(
            text(
                "SELECT count(*) FROM plaid.transactions "
                "WHERE plaid_transaction_id = 'adv-concurrent-txn'"
            )
        ).scalar_one()
        final = get_by_plaid_id(verify, "adv-concurrent-txn")
        assert final is not None
        final_amount = final.amount
        verify.execute(
            text("DELETE FROM plaid.transactions WHERE plaid_transaction_id = 'adv-concurrent-txn'")
        )
        verify.commit()
    fresh_engine.dispose()

    assert count == 1, "concurrent upserts of the same id must not create duplicate rows"
    assert final_amount in (Decimal("10.00"), Decimal("20.00"))


# ---------------------------------------------------------------------------
# Pending -> posted transition
# ---------------------------------------------------------------------------


def test_pending_to_posted_same_id_updates_pending_flag_in_place(app_session) -> None:
    """Plaid sometimes reissues a posted transaction under the *same*
    plaid_transaction_id it used while pending. This shape works today
    because upsert's ON CONFLICT clause updates every mutable field
    including `pending` and `date`/`authorized_date`."""
    session, account_id = app_session
    upsert(
        session,
        _fields(
            account_id,
            plaid_transaction_id="adv-pend-1",
            pending=True,
            amount=Decimal("6.25"),
            authorized_date=datetime.date(2026, 1, 4),
        ),
    )
    session.commit()

    posted = upsert(
        session,
        _fields(
            account_id,
            plaid_transaction_id="adv-pend-1",
            pending=False,
            amount=Decimal("6.30"),  # tip adjustment settled at posting
            authorized_date=datetime.date(2026, 1, 4),
            date=datetime.date(2026, 1, 5),
        ),
    )
    session.commit()

    assert posted.pending is False
    assert posted.amount == Decimal("6.30")
    row_count = session.execute(
        text("SELECT count(*) FROM plaid.transactions WHERE plaid_transaction_id = 'adv-pend-1'")
    ).scalar_one()
    assert row_count == 1


def test_pending_to_posted_new_id_requires_caller_to_remove_the_pending_row(
    app_session,
) -> None:
    """Plaid also documents the opposite shape: the pending transaction
    gets a *new* plaid_transaction_id when it posts, and the old pending id
    is reported in the same page's `removed` list. The repository layer
    has no way to link the two — that linkage is entirely Milestone 2's
    responsibility. This test proves that if the sync loop forgets to call
    `mark_removed` on the old id, the pending row is left live forever,
    alongside the new posted row — a silent double-count of the same
    real-world purchase."""
    session, account_id = app_session

    upsert(
        session,
        _fields(
            account_id,
            plaid_transaction_id="adv-pend-old",
            pending=True,
            amount=Decimal("6.25"),
        ),
    )
    session.commit()

    # New posted transaction arrives under a different id; caller "forgets"
    # to mark the old pending id removed (a plausible Milestone 2 bug).
    upsert(
        session,
        _fields(
            account_id,
            plaid_transaction_id="adv-pend-new",
            pending=False,
            amount=Decimal("6.30"),
        ),
    )
    session.commit()

    old = get_by_plaid_id(session, "adv-pend-old")
    new = get_by_plaid_id(session, "adv-pend-new")
    assert old is not None and old.removed_at is None, (
        "the repository layer does nothing to prevent a double-counted "
        "transaction when a pending->posted transition changes ids; "
        "Milestone 2 MUST call mark_removed on the old id explicitly"
    )
    assert new is not None
    # Both rows are simultaneously active and would both be summed by any
    # naive SUM(amount) query — a real double-count if this occurs live.
    total = old.amount + new.amount
    assert total == Decimal("12.55")
