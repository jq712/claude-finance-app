"""End-to-end tests for `finance_app.plaid.sync.run_sync` against a real
PostgreSQL test container, using a fake `SyncClient` (no network/Plaid
Sandbox dependency — see tests/plaid_fixtures/synthetic.py for the shapes).

Per handoff §30, the two questions that matter for ingestion are: can an
interruption lose an update, and can a re-run duplicate a row? These tests
answer both, plus the added/modified/removed/pagination lifecycle.
"""

import datetime
import json
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import plaid
import pytest
from sqlalchemy import text

from finance_app.db.session import get_sessionmaker, session_scope
from finance_app.plaid.sync import PlaidSyncError, SyncClient, SyncRunSummary, run_sync

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


# ---------------------------------------------------------------------------
# Adversarial QA pass — concurrency, pagination edge cases, pending/posted
# transitions, and finalization-boundary crashes. See the review report for
# severity ranking; each test below stands on its own as a reproduction.
# ---------------------------------------------------------------------------


def test_concurrent_sync_invocations_fail_fast_instead_of_regressing_the_cursor() -> None:
    """Two overlapping `run_sync` invocations (e.g. a systemd timer firing
    while a manual `finance sync` is still running) used to interleave: each
    read `plaid.sync_state.cursor` independently and later wrote its own
    `next_cursor` back unconditionally, letting a slower run's stale write
    land after a faster run's and silently regress the cursor — see
    `docs/plaid-sync.md`'s Concurrency section. `run_sync` now holds a
    Postgres advisory lock for its whole duration; a second invocation must
    fail fast with `PlaidSyncError` rather than proceed and corrupt shared
    state."""
    b_reached_network = threading.Event()
    b_may_finish = threading.Event()

    class BlockingFirstPageClient(FakeSyncClient):
        def transactions_sync(self, transactions_sync_request):
            self.cursors_requested.append(getattr(transactions_sync_request, "cursor", None))
            b_reached_network.set()
            assert b_may_finish.wait(timeout=5), "test deadlocked waiting to be released"
            page = self._pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page

    a_client = FakeSyncClient(
        [
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-a1")],
                next_cursor="cA1",
                has_more=True,
            ),
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-a2")],
                next_cursor="cA2",
                has_more=False,
            ),
        ]
    )
    b_client = BlockingFirstPageClient(
        [
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-b1")],
                next_cursor="cB1",
                has_more=False,
            )
        ]
    )

    b_results: list[SyncRunSummary] = []

    def _run_b() -> None:
        b_results.append(run_sync(b_client, access_token="tok", run_type="scheduled"))

    b_thread = threading.Thread(target=_run_b)
    b_thread.start()
    assert b_reached_network.wait(timeout=5), "run B never reached its network call"

    # B holds the advisory lock for its entire run, including while it is
    # blocked mid-page here — A must fail fast rather than block or proceed.
    with pytest.raises(PlaidSyncError, match="already in progress"):
        run_sync(a_client, access_token="tok", run_type="manual")

    b_may_finish.set()
    b_thread.join(timeout=5)
    assert not b_thread.is_alive(), "run B did not finish"
    assert b_results[0].status == "success"

    Session = get_sessionmaker()
    with Session() as session:
        cursor = session.execute(text("SELECT cursor FROM plaid.sync_state")).scalar_one()

    # A never wrote anything -- it was rejected before touching the
    # database at all -- so B's cursor and rows are exactly as B left them.
    assert cursor == "cB1"
    assert _counts()["plaid.transactions"] == 1
    assert a_client.item_get_calls == 0, "A must be rejected before it ever calls Plaid"


def test_a_later_page_with_no_accounts_list_still_resolves_a_previously_seen_account() -> None:
    """Plaid does not resend the full `accounts` array on every page of a
    single sync run -- only entries relevant to that page. A page whose
    `accounts` list is empty, but whose `modified` list references an
    account introduced by an *earlier* page in the same run, must resolve
    that account from what's already committed rather than raising
    `PlaidSyncError` as if the account were genuinely unknown."""
    client = FakeSyncClient(
        [
            _Page(
                accounts=[_fake_account()],
                added=[_fake_txn("txn-1")],
                next_cursor="c1",
                has_more=True,
            ),
            _Page(
                accounts=[],
                modified=[_fake_txn("txn-1", amount=Decimal("15.00"))],
                next_cursor="c2",
                has_more=False,
            ),
        ]
    )

    summary = run_sync(client, access_token="tok")

    assert summary.status == "success"
    assert summary.modified_count == 1
    assert _counts()["plaid.transactions"] == 1


def test_pending_to_posted_transition_with_a_different_id_in_the_same_page() -> None:
    """Plaid can represent a pending -> posted transition as a brand new
    transaction id: the old pending id shows up in `removed`, the new
    posted id shows up in `added`, in the same page. Losing either half —
    failing to tombstone the old id, or failing to insert the new one —
    would silently lose or duplicate the transaction."""
    run_sync(
        FakeSyncClient(
            [
                _Page(
                    accounts=[_fake_account()],
                    added=[_fake_txn("txn-pending", pending=True)],
                    next_cursor="c1",
                )
            ]
        ),
        access_token="tok",
    )

    run_sync(
        FakeSyncClient(
            [
                _Page(
                    accounts=[_fake_account()],
                    added=[_fake_txn("txn-posted", pending=False)],
                    removed=[_fake_removed("txn-pending")],
                    next_cursor="c2",
                )
            ]
        ),
        access_token="tok",
    )

    Session = get_sessionmaker()
    with Session() as session:
        pending_removed_at = session.execute(
            text(
                "SELECT removed_at FROM plaid.transactions "
                "WHERE plaid_transaction_id = 'txn-pending'"
            )
        ).scalar_one()
        posted_removed_at = session.execute(
            text(
                "SELECT removed_at FROM plaid.transactions "
                "WHERE plaid_transaction_id = 'txn-posted'"
            )
        ).scalar_one_or_none()

    assert pending_removed_at is not None, "the old pending id must be tombstoned"
    assert posted_removed_at is None, "the new posted id must be live, not itself tombstoned"
    assert _counts()["plaid.transactions"] == 2, "both rows retained -- provenance, not deletion"


def test_removed_for_a_never_seen_transaction_id_is_a_silent_no_op_end_to_end() -> None:
    """docs/plaid-sync.md documents a known limitation: a `removed` event
    for a transaction id this database has never seen is a silent no-op,
    not remembered as a tombstone. Confirmed here through the real
    `run_sync` entry point (not just the repository layer): the run
    succeeds, nothing is inserted, and — because no tombstone is
    remembered — a later `added` for that same id inserts it live rather
    than being suppressed."""
    summary = run_sync(
        FakeSyncClient(
            [
                _Page(
                    accounts=[_fake_account()],
                    removed=[_fake_removed("ghost-txn")],
                    next_cursor="c1",
                )
            ]
        ),
        access_token="tok",
    )

    assert summary.status == "success"
    assert summary.removed_count == 1
    assert _counts()["plaid.transactions"] == 0

    run_sync(
        FakeSyncClient(
            [_Page(accounts=[_fake_account()], added=[_fake_txn("ghost-txn")], next_cursor="c2")]
        ),
        access_token="tok",
    )

    Session = get_sessionmaker()
    with Session() as session:
        removed_at = session.execute(
            text(
                "SELECT removed_at FROM plaid.transactions WHERE plaid_transaction_id = 'ghost-txn'"
            )
        ).scalar_one()
    assert removed_at is None, "no phantom tombstone -- the late add inserts live"


def test_retries_exhausted_mid_run_records_failure_without_leaking_the_access_token() -> None:
    """When retries are exhausted on page 2 (after page 1 already
    committed), the failure path must (a) leave the cursor at page 1's
    committed value, (b) mark both `plaid.sync_state` and `ops.sync_runs`
    as failed, and (c) never persist the access token or any raw header
    from the underlying `ApiException` into the stored error message."""

    def _rate_limit_exc() -> plaid.ApiException:
        exc = plaid.ApiException(status=429, reason="Too Many Requests")
        exc.body = json.dumps(
            {
                "error_type": "RATE_LIMIT_EXCEEDED",
                "error_code": "RATE_LIMIT_EXCEEDED",
                "error_message": "rate limited for access_token=super-secret-access-token-xyz",
            }
        )
        exc.headers = {"Authorization": "Bearer super-secret-access-token-xyz"}
        return exc

    good_page = _Page(
        accounts=[_fake_account()],
        added=[_fake_txn("txn-1")],
        next_cursor="c1",
        has_more=True,
    )
    client = FakeSyncClient([good_page, _rate_limit_exc(), _rate_limit_exc(), _rate_limit_exc()])

    with pytest.raises(PlaidSyncError):
        run_sync(client, access_token="super-secret-access-token-xyz")

    Session = get_sessionmaker()
    with Session() as session:
        state_row = session.execute(
            text("SELECT cursor, status, last_error FROM plaid.sync_state")
        ).one()
        run_row = session.execute(text("SELECT status, last_error FROM ops.sync_runs")).one()

    assert state_row.cursor == "c1", "cursor stays at the last durably committed page"
    assert state_row.status == "error"
    assert run_row.status == "error"
    for message in (state_row.last_error, run_row.last_error):
        assert message is not None
        assert "super-secret-access-token-xyz" not in message
        assert "Bearer" not in message


def test_kill_between_last_page_commit_and_finalization_leaves_status_stuck() -> None:
    """The last page's row-writes + cursor advance commit in one
    transaction; `record_success`/`finish_success` commit in *separate*
    transactions afterward (see plaid/sync.py). A process killed in the
    gap between them leaves `plaid.sync_state.status` and
    `ops.sync_runs.status` stuck at `'running'` forever, even though the
    data and the cursor are already correct and durable. This directly
    replicates that gap by driving the same repository calls run_sync
    uses and stopping short of finalization -- simulating the kill -- then
    proves two things: (1) no data was lost, and (2) the stuck `'running'`
    status is not a lock -- nothing in the loop checks it, so the very
    next real sync proceeds normally and does not duplicate the row."""
    from finance_app.db.repositories import accounts as accounts_repo
    from finance_app.db.repositories import items as items_repo
    from finance_app.db.repositories import sync_runs as sync_runs_repo
    from finance_app.db.repositories import sync_state as sync_state_repo
    from finance_app.db.repositories import transactions as transactions_repo
    from finance_app.db.repositories.transactions import TransactionFields

    with session_scope() as session:
        item = items_repo.upsert_item(
            session, plaid_item_id="killed-item", institution_id="ins", institution_name="Bank"
        )
        item_id = item.id

    with session_scope() as session:
        sync_runs_repo.start(session, item_id=item_id, run_type="manual")

    with session_scope() as session:
        state = sync_state_repo.get_or_create(session, item_id=item_id)
        sync_state_repo.record_attempt(session, state)

    with session_scope() as session:
        state = sync_state_repo.get_or_create(session, item_id=item_id)
        account = accounts_repo.upsert_account(
            session,
            item_id=item_id,
            plaid_account_id="acc-killed",
            name="Killed Checking",
            official_name=None,
            type="depository",
            subtype="checking",
            mask="0000",
            iso_currency_code="USD",
        )
        transactions_repo.upsert(
            session,
            TransactionFields(
                plaid_transaction_id="txn-killed",
                account_id=account.id,
                amount=Decimal("5.00"),
                date=datetime.date(2026, 1, 1),
                name="SURVIVES A KILL",
            ),
        )
        sync_state_repo.advance(
            session,
            state,
            cursor="final-cursor",
            added_count=1,
            modified_count=0,
            removed_count=0,
            request_id="req-1",
        )
    # --- simulated SIGKILL here: record_success / finish_success never run. ---

    Session = get_sessionmaker()
    with Session() as session:
        state_row = session.execute(text("SELECT cursor, status FROM plaid.sync_state")).one()
        run_row = session.execute(text("SELECT status FROM ops.sync_runs")).one()

    assert state_row.cursor == "final-cursor", "the data-bearing commit landed"
    assert state_row.status == "running", "finalization never ran -- status is stuck"
    assert run_row.status == "running"
    assert _counts()["plaid.transactions"] == 1

    resumed_client = FakeSyncClient(
        [
            _Page(
                accounts=[_fake_account(account_id="acc-killed")],
                next_cursor="final-cursor-2",
                has_more=False,
            )
        ]
    )
    summary = run_sync(resumed_client, access_token="tok")

    assert summary.status == "success"
    assert resumed_client.cursors_requested == ["final-cursor"], (
        "resumed from the last durably committed cursor, unblocked by the "
        "stuck 'running' status from the 'killed' run"
    )
    assert _counts()["plaid.transactions"] == 1, "no duplication of the row from the 'killed' run"
