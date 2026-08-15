"""End-to-end tests for `finance_app.plaid.sync.run_sync` against a real
PostgreSQL test container, using a fake `SyncClient` (no network/Plaid
Sandbox dependency — see tests/plaid_fixtures/synthetic.py for the shapes).

Per handoff §30, the two questions that matter for ingestion are: can an
interruption lose an update, and can a re-run duplicate a row? These tests
answer both, plus the added/modified/removed/pagination lifecycle.
"""

import datetime
import json
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import plaid
import pytest
from sqlalchemy import text

from finance_app.db.session import get_sessionmaker
from finance_app.plaid.sync import PlaidSyncError, SyncClient, run_sync

pytestmark = pytest.mark.integration

_TABLES_IN_FK_ORDER = (
    "ops.sync_runs",
    "plaid.sync_state",
    "plaid.transactions",
    "plaid.accounts",
    "plaid.items",
)


@pytest.fixture(autouse=True)
def clean_plaid_tables():
    """This application connects exactly one Plaid Item (handoff §2), and
    `_ensure_item` picks whatever row is already there — so each test must
    start and end with a clean slate rather than trying to coexist with
    another test's Item."""
    Session = get_sessionmaker()

    def _wipe() -> None:
        with Session() as session:
            for table in _TABLES_IN_FK_ORDER:
                session.execute(text(f"DELETE FROM {table}"))
            session.commit()

    _wipe()
    yield
    _wipe()


def _fake_account(
    account_id: str = "acc-checking",
    name: str = "Synthetic Checking",
    iso_currency_code: str | None = "USD",
) -> SimpleNamespace:
    return SimpleNamespace(
        account_id=account_id,
        name=name,
        official_name=None,
        type="depository",
        subtype="checking",
        mask="1234",
        balances=SimpleNamespace(iso_currency_code=iso_currency_code),
    )


def _fake_txn(
    transaction_id: str,
    *,
    account_id: str = "acc-checking",
    amount: Decimal = Decimal("10.00"),
    date: datetime.date = datetime.date(2026, 1, 5),
    name: str = "SYNTHETIC MERCHANT",
    pending: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id=transaction_id,
        account_id=account_id,
        amount=amount,
        date=date,
        authorized_date=None,
        name=name,
        merchant_name=None,
        pending=pending,
        payment_channel="other",
        iso_currency_code="USD",
        category=None,
        personal_finance_category=None,
    )


def _fake_removed(transaction_id: str, account_id: str = "acc-checking") -> SimpleNamespace:
    return SimpleNamespace(transaction_id=transaction_id, account_id=account_id)


@dataclass
class _Page:
    accounts: list = field(default_factory=list)
    added: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    next_cursor: str = "cursor"
    has_more: bool = False
    request_id: str = "req-id"


class FakeSyncClient(SyncClient):
    """A `SyncClient` that serves a scripted sequence of pages and records
    the cursor each `transactions_sync` call was made with, so tests can
    assert exactly what was (and wasn't) requested."""

    def __init__(
        self,
        pages: list[_Page | Exception],
        *,
        item_id: str = "fake-item",
        institution_id: str = "ins_fake",
        institution_name: str = "Fake Bank",
        item_get_exception: Exception | None = None,
    ) -> None:
        self._pages = list(pages)
        self._item_id = item_id
        self._institution_id = institution_id
        self._institution_name = institution_name
        self._item_get_exception = item_get_exception
        self.cursors_requested: list[str | None] = []
        self.item_get_calls = 0

    def item_get(self, item_get_request):
        self.item_get_calls += 1
        if self._item_get_exception is not None:
            raise self._item_get_exception
        return SimpleNamespace(
            item=SimpleNamespace(
                item_id=self._item_id,
                institution_id=self._institution_id,
                institution_name=self._institution_name,
            )
        )

    def transactions_sync(self, transactions_sync_request):
        # Plaid's request model omits `cursor` entirely (rather than setting
        # it to `None`) for the initial sync — see plaid/sync.py.
        self.cursors_requested.append(getattr(transactions_sync_request, "cursor", None))
        page = self._pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _counts() -> dict[str, int]:
    Session = get_sessionmaker()
    with Session() as session:
        return {
            table: session.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed allowlist, no input
            ).scalar_one()
            for table in ("plaid.items", "plaid.accounts", "plaid.transactions")
        }


def test_first_sync_bootstraps_item_account_and_added_transactions() -> None:
    client = FakeSyncClient(
        [
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-1"), _fake_txn("txn-2", amount=Decimal("-3200.00"))],
                next_cursor="c1",
                has_more=False,
            )
        ]
    )

    summary = run_sync(client, access_token="tok", run_type="manual")

    assert summary.status == "success"
    assert (summary.added_count, summary.modified_count, summary.removed_count) == (2, 0, 0)
    assert summary.pages == 1
    assert client.item_get_calls == 1
    assert _counts() == {"plaid.items": 1, "plaid.accounts": 1, "plaid.transactions": 2}

    Session = get_sessionmaker()
    with Session() as session:
        cursor = session.execute(text("SELECT cursor FROM plaid.sync_state")).scalar_one()
        assert cursor == "c1"
        run_row = session.execute(text("SELECT status, added_count FROM ops.sync_runs")).one()
        assert run_row.status == "success"
        assert run_row.added_count == 2


def test_pagination_advances_cursor_and_accumulates_across_pages() -> None:
    client = FakeSyncClient(
        [
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-1")],
                next_cursor="c1",
                has_more=True,
            ),
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-2")],
                next_cursor="c2",
                has_more=False,
            ),
        ]
    )

    summary = run_sync(client, access_token="tok")

    assert summary.pages == 2
    assert summary.added_count == 2
    assert client.cursors_requested == [None, "c1"]
    assert _counts()["plaid.transactions"] == 2

    Session = get_sessionmaker()
    with Session() as session:
        cursor = session.execute(text("SELECT cursor FROM plaid.sync_state")).scalar_one()
        assert cursor == "c2"


def test_second_run_does_not_refetch_item_and_updates_modified_transaction_in_place() -> None:
    run_sync(
        FakeSyncClient(
            [_Page(accounts=[_fake_account()], added=[_fake_txn("txn-1", amount=Decimal("10.00"))])]
        ),
        access_token="tok",
    )

    second_client = FakeSyncClient(
        [
            _Page(
                accounts=[_fake_account()],
                modified=[_fake_txn("txn-1", amount=Decimal("20.00"), pending=True)],
                next_cursor="c2",
            )
        ]
    )
    summary = run_sync(second_client, access_token="tok")

    assert second_client.item_get_calls == 0, "item is bootstrapped once, not every sync"
    assert summary.modified_count == 1
    assert _counts()["plaid.transactions"] == 1, "modified must update in place, not duplicate"

    Session = get_sessionmaker()
    with Session() as session:
        row = session.execute(
            text(
                "SELECT amount, pending FROM plaid.transactions "
                "WHERE plaid_transaction_id = 'txn-1'"
            )
        ).one()
        assert row.amount == Decimal("20.00")
        assert row.pending is True


def test_removed_transaction_is_tombstoned_not_deleted() -> None:
    run_sync(
        FakeSyncClient([_Page(accounts=[_fake_account()], added=[_fake_txn("txn-1")])]),
        access_token="tok",
    )

    run_sync(
        FakeSyncClient(
            [_Page(accounts=[_fake_account()], removed=[_fake_removed("txn-1")], next_cursor="c2")]
        ),
        access_token="tok",
    )

    assert _counts()["plaid.transactions"] == 1, "removed rows are tombstoned, never deleted"
    Session = get_sessionmaker()
    with Session() as session:
        removed_at = session.execute(
            text("SELECT removed_at FROM plaid.transactions WHERE plaid_transaction_id = 'txn-1'")
        ).scalar_one()
        assert removed_at is not None


def test_a_failed_page_leaves_the_cursor_at_the_last_good_page_and_the_retry_resumes_there() -> (
    None
):
    """The core cursor-durability property: if page 2 fails after page 1
    committed, the stored cursor must still be page 1's `next_cursor` — not
    left at `None` (which would re-fetch page 1 and duplicate it) and not
    advanced to page 2's `next_cursor` (which would skip page 2's data
    forever). The next sync must resume exactly at page 1's cursor."""
    good_page = _Page(
        accounts=[_fake_account()],
        added=[_fake_txn("txn-good")],
        next_cursor="c1",
        has_more=True,
    )
    # References an account that was never included in this page's
    # `accounts` list — an impossible-in-practice Plaid response, used here
    # purely to force a mid-page failure after page 1 has already committed.
    breaking_page = _Page(
        accounts=[],
        added=[_fake_txn("txn-bad", account_id="never-upserted")],
        next_cursor="c2",
        has_more=False,
    )
    client = FakeSyncClient([good_page, breaking_page])

    with pytest.raises(PlaidSyncError):
        run_sync(client, access_token="tok")

    Session = get_sessionmaker()
    with Session() as session:
        cursor = session.execute(text("SELECT cursor FROM plaid.sync_state")).scalar_one()
        assert cursor == "c1", "cursor must not advance past the page that failed to commit"
        run_status = session.execute(text("SELECT status FROM ops.sync_runs")).scalar_one()
        assert run_status == "error"

    assert _counts()["plaid.transactions"] == 1, "only the successfully committed page's row exists"

    # Retry: a fixed page 2 arrives. The retry must resume from "c1", the
    # last *committed* cursor — proving the failure did not lose the page.
    fixed_page = _Page(
        accounts=[_fake_account()],
        added=[_fake_txn("txn-bad", account_id="acc-checking")],
        next_cursor="c2",
        has_more=False,
    )
    retry_client = FakeSyncClient([fixed_page])
    summary = run_sync(retry_client, access_token="tok")

    assert retry_client.cursors_requested == ["c1"]
    assert summary.status == "success"
    assert _counts()["plaid.transactions"] == 2


def test_bootstrap_item_get_failure_is_sanitized_before_it_can_leak() -> None:
    """A raw `plaid.ApiException` carries the full HTTP response body and
    headers in `str(exc)` — security-model.md invariant 6 forbids
    persisting or displaying that unsanitized. `/item/get` on first-run
    bootstrap must translate it to a `PlaidSyncError` containing only the
    Plaid `error_type`, exactly like every other Plaid call site does."""
    sensitive_body = json.dumps(
        {
            "error_type": "ITEM_LOGIN_REQUIRED",
            "error_code": "ITEM_LOGIN_REQUIRED",
            "error_message": "the access token is definitely-not-a-real-secret-value",
        }
    )
    exc = plaid.ApiException(status=400, reason="Bad Request")
    exc.body = sensitive_body
    exc.headers = {"Authorization": "should-never-appear-in-a-sanitized-message"}

    client = FakeSyncClient([], item_get_exception=exc)

    with pytest.raises(PlaidSyncError) as excinfo:
        run_sync(client, access_token="tok")

    message = str(excinfo.value)
    assert "ITEM_LOGIN_REQUIRED" in message
    assert "definitely-not-a-real-secret-value" not in message
    assert "should-never-appear-in-a-sanitized-message" not in message
    assert _counts() == {"plaid.items": 0, "plaid.accounts": 0, "plaid.transactions": 0}
