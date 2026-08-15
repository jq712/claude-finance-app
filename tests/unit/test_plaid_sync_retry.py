"""Unit tests for the retry/backoff behavior around `/transactions/sync`.
No database or network involved — see tests/integration/test_plaid_sync.py
for the end-to-end ingestion behavior."""

import json
from typing import cast

import plaid
import pytest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_get_response import ItemGetResponse
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_response import TransactionsSyncResponse

from finance_app.plaid.sync import PlaidSyncError, _sync_page_with_retry


def _api_exception(error_type: str) -> plaid.ApiException:
    exc = plaid.ApiException(status=500, reason="error")
    exc.body = json.dumps({"error_type": error_type, "error_code": error_type})
    return exc


class _FlakyClient:
    """A minimal `SyncClient`. Real Plaid response objects are heavyweight
    to construct, so the "response" here is just a sentinel identity check
    — this test is about retry/backoff control flow, not payload shape."""

    def __init__(self, exceptions: list[Exception], final_response: object) -> None:
        self._exceptions = list(exceptions)
        self._final_response = final_response
        self.calls = 0

    def item_get(self, item_get_request: ItemGetRequest) -> ItemGetResponse:  # pragma: no cover
        raise NotImplementedError

    def transactions_sync(
        self, transactions_sync_request: TransactionsSyncRequest
    ) -> TransactionsSyncResponse:
        self.calls += 1
        if self._exceptions:
            raise self._exceptions.pop(0)
        return cast(TransactionsSyncResponse, self._final_response)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("finance_app.plaid.sync.time.sleep", lambda _seconds: None)


def test_retries_transient_error_then_succeeds() -> None:
    sentinel = object()
    client = _FlakyClient([_api_exception("RATE_LIMIT_EXCEEDED")], sentinel)

    result = _sync_page_with_retry(client, access_token="tok", cursor=None)

    assert result is sentinel
    assert client.calls == 2


def test_non_retryable_error_raises_immediately_without_further_attempts() -> None:
    client = _FlakyClient([_api_exception("ITEM_LOGIN_REQUIRED")], object())

    with pytest.raises(PlaidSyncError):
        _sync_page_with_retry(client, access_token="tok", cursor=None)

    assert client.calls == 1


def test_exhausts_retries_and_raises() -> None:
    client = _FlakyClient(
        [
            _api_exception("RATE_LIMIT_EXCEEDED"),
            _api_exception("RATE_LIMIT_EXCEEDED"),
            _api_exception("RATE_LIMIT_EXCEEDED"),
        ],
        object(),
    )

    with pytest.raises(PlaidSyncError):
        _sync_page_with_retry(client, access_token="tok", cursor=None)

    assert client.calls == 3


def test_non_api_exception_bypasses_retry_entirely() -> None:
    """`_sync_page_with_retry` only catches `plaid.ApiException`. A raw
    transport-level failure that the SDK does not wrap as an `ApiException`
    (a DNS failure, a connection reset, a read timeout surfaced as a bare
    `ConnectionError`/`TimeoutError`) is therefore not retried at all -- it
    propagates immediately as its original type on the very first attempt,
    contrary to the module's stated intent of retrying transient failures.
    It also isn't wrapped as `PlaidSyncError`, so a caller that only catches
    `PlaidSyncError` (as `finance sync`'s CLI handler does) would not catch
    this and would crash with an unhandled exception instead."""
    client = _FlakyClient([ConnectionError("connection reset by peer")], object())

    with pytest.raises(ConnectionError):
        _sync_page_with_retry(client, access_token="tok", cursor=None)

    assert client.calls == 1, "no retry attempted for a non-ApiException failure"
