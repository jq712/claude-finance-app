"""The `/transactions/sync` ingestion loop — the only code path permitted
to write raw `plaid.*` facts (handoff §4.1, §6). See docs/plaid-sync.md for
the full design and docs/runbooks/plaid-link.md for how the single Item's
access token is obtained.

Cursor durability (handoff §6.2): for each page, the account/transaction
row writes and the cursor advance happen inside one database transaction.
The cursor in `plaid.sync_state` therefore only ever points past data that
is durably committed. If the process is killed at any point before that
commit, the next run resumes from the last *committed* cursor and
re-fetches/re-processes that page. Re-processing is safe because:

- `transactions.upsert` is keyed on `plaid_transaction_id` (insert-or-update,
  never a duplicate row);
- `transactions.mark_removed` is an idempotent tombstone, never a hard
  delete;
- `accounts.upsert_account` is keyed on `plaid_account_id`.

Per Plaid's guidance, a failed page is retried from the cursor that was
current *before* that page's request, never from a `next_cursor` whose
page might not have been durably processed.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast

import plaid
from plaid.model.accounts_get_response import AccountsGetResponse
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_get_response import ItemGetResponse
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_response import TransactionsSyncResponse
from sqlalchemy.orm import Session

from finance_app.config.settings import get_settings
from finance_app.db.models.ops import SyncRun
from finance_app.db.models.plaid import Item
from finance_app.db.repositories import accounts, items, sync_runs, sync_state, transactions
from finance_app.db.repositories.transactions import TransactionFields
from finance_app.db.session import session_scope
from finance_app.plaid.client import build_client

logger = logging.getLogger(__name__)

_RETRYABLE_ERROR_TYPES = {"RATE_LIMIT_EXCEEDED", "API_ERROR"}
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0


class SyncClient(Protocol):
    """The subset of `plaid_api.PlaidApi` this module calls. A Protocol
    (rather than importing `PlaidApi` directly) so tests can substitute a
    fake without touching the network — see tests/unit/test_plaid_sync.py."""

    def item_get(self, item_get_request: ItemGetRequest) -> ItemGetResponse: ...

    def transactions_sync(
        self, transactions_sync_request: TransactionsSyncRequest
    ) -> TransactionsSyncResponse: ...


@dataclass(frozen=True, slots=True)
class SyncRunSummary:
    run_id: int
    status: str
    added_count: int
    modified_count: int
    removed_count: int
    pages: int


class PlaidSyncError(RuntimeError):
    """A `/transactions/sync` call failed after exhausting retries."""


def _error_type(exc: plaid.ApiException) -> str | None:
    import json

    try:
        body = json.loads(exc.body) if exc.body else {}
    except (TypeError, ValueError):
        return None
    return body.get("error_type")


def _sync_page_with_retry(
    client: SyncClient, *, access_token: str, cursor: str | None
) -> TransactionsSyncResponse:
    """Calls `/transactions/sync` for one page, retrying transient Plaid
    errors with backoff. Always retries from the same `cursor` — never a
    partially-received `next_cursor` — so a retry cannot skip data."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            # The SDK's TransactionsSyncRequest type-validates `cursor` as
            # `str`, rejecting `None` outright — Plaid's documented way to
            # request the initial page is to omit the field entirely.
            kwargs = {"access_token": access_token}
            if cursor is not None:
                kwargs["cursor"] = cursor
            request = cast(TransactionsSyncRequest, TransactionsSyncRequest(**kwargs))
            return client.transactions_sync(request)
        except plaid.ApiException as exc:
            last_exc = exc
            error_type = _error_type(exc)
            if error_type not in _RETRYABLE_ERROR_TYPES or attempt == _MAX_ATTEMPTS:
                raise PlaidSyncError(f"transactions_sync failed (error_type={error_type})") from exc
            logger.warning(
                "plaid.transactions_sync transient error, retrying",
                extra={"attempt": attempt, "error_type": error_type},
            )
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise PlaidSyncError("transactions_sync failed") from last_exc


def _ensure_item(session: Session, client: SyncClient, *, access_token: str) -> Item:
    """Returns the single Item this application connects, creating it from
    Plaid's `/item/get` on first run only. Subsequent syncs make no extra
    API call here — account metadata is refreshed from the `accounts` field
    already present on every `/transactions/sync` response."""
    existing = session.query(Item).order_by(Item.id).first()
    if existing is not None:
        return existing
    request = cast(ItemGetRequest, ItemGetRequest(access_token=access_token))
    response = client.item_get(request)
    return items.upsert_item(
        session,
        plaid_item_id=response.item.item_id,
        institution_id=response.item.institution_id,
        institution_name=response.item.institution_name,
    )


def _upsert_accounts_from_response(
    session: Session, *, item_id: int, response: TransactionsSyncResponse | AccountsGetResponse
) -> None:
    for acct in response.accounts:
        iso_currency_code = None
        if acct.balances is not None:
            iso_currency_code = acct.balances.iso_currency_code
        accounts.upsert_account(
            session,
            item_id=item_id,
            plaid_account_id=acct.account_id,
            name=acct.name,
            official_name=acct.official_name,
            type=str(acct.type),
            subtype=str(acct.subtype) if acct.subtype is not None else None,
            mask=acct.mask,
            iso_currency_code=iso_currency_code,
        )


def _account_pk(session: Session, *, plaid_account_id: str) -> int:
    account = accounts.get_by_plaid_account_id(session, plaid_account_id)
    if account is None:
        # The sync response's `accounts` list is upserted in the same
        # transaction just above, so this should be unreachable.
        raise PlaidSyncError(f"transaction references unknown account {plaid_account_id!r}")
    return account.id


def run_sync(client: SyncClient, *, access_token: str, run_type: str = "manual") -> SyncRunSummary:
    """Runs `/transactions/sync` to completion (all pages) for the single
    configured Item, recording an `ops.sync_runs` audit row and advancing
    `plaid.sync_state` page by page. Raises `PlaidSyncError` on failure;
    the audit row and sync state are still updated to reflect the failure
    before the exception propagates."""
    with session_scope() as session:
        item = _ensure_item(session, client, access_token=access_token)
        item_id = item.id

    with session_scope() as session:
        run = sync_runs.start(session, item_id=item_id, run_type=run_type)
        run_id = run.id

    total_added = total_modified = total_removed = 0
    pages = 0
    last_request_id: str | None = None

    try:
        while True:
            with session_scope() as session:
                state = sync_state.get_or_create(session, item_id=item_id)
                sync_state.record_attempt(session, state)
                cursor = state.cursor

            response = _sync_page_with_retry(client, access_token=access_token, cursor=cursor)
            pages += 1

            with session_scope() as session:
                state = sync_state.get_or_create(session, item_id=item_id)
                _upsert_accounts_from_response(session, item_id=item_id, response=response)

                for txn in list(response.added) + list(response.modified):
                    account_pk = _account_pk(session, plaid_account_id=txn.account_id)
                    transactions.upsert(
                        session,
                        TransactionFields(
                            plaid_transaction_id=txn.transaction_id,
                            account_id=account_pk,
                            amount=Decimal(str(txn.amount)),
                            date=txn.date,
                            name=txn.name,
                            iso_currency_code=txn.iso_currency_code,
                            authorized_date=txn.authorized_date,
                            merchant_name=txn.merchant_name,
                            pending=txn.pending,
                            payment_channel=txn.payment_channel,
                            plaid_category=list(txn.category) if txn.category else None,
                            personal_finance_category=(
                                txn.personal_finance_category.to_dict()
                                if txn.personal_finance_category is not None
                                else None
                            ),
                        ),
                    )

                for removed in response.removed:
                    transactions.mark_removed(session, removed.transaction_id)

                total_added += len(response.added)
                total_modified += len(response.modified)
                total_removed += len(response.removed)
                last_request_id = response.request_id

                sync_state.advance(
                    session,
                    state,
                    cursor=response.next_cursor,
                    added_count=total_added,
                    modified_count=total_modified,
                    removed_count=total_removed,
                    request_id=last_request_id,
                )

            if not response.has_more:
                break

        with session_scope() as session:
            state = sync_state.get_or_create(session, item_id=item_id)
            sync_state.record_success(session, state)

        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            sync_runs.finish_success(
                session,
                run,
                added_count=total_added,
                modified_count=total_modified,
                removed_count=total_removed,
                request_id=last_request_id,
            )

        return SyncRunSummary(
            run_id=run_id,
            status="success",
            added_count=total_added,
            modified_count=total_modified,
            removed_count=total_removed,
            pages=pages,
        )
    except Exception as exc:
        error_message = str(exc)
        with session_scope() as session:
            state = sync_state.get_or_create(session, item_id=item_id)
            sync_state.record_failure(session, state, error=error_message)
        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            assert run is not None
            sync_runs.finish_failure(session, run, error=error_message)
        raise


def run_daily_sync() -> SyncRunSummary:
    """Entry point for the daily systemd timer / `finance sync` CLI
    command. Builds the Plaid client and reads the access token from
    `Settings` — never a default, never a hardcoded value."""

    settings = get_settings()
    access_token = settings.plaid_access_token.get_secret_value()
    if not access_token:
        raise PlaidSyncError("PLAID_ACCESS_TOKEN is not set")
    client = build_client(settings)
    return run_sync(client, access_token=access_token, run_type="scheduled")
