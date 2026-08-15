# Plaid transaction sync

How `finance sync` (and the future `finance-sync.timer`, Milestone 7) keeps
`plaid.*` in step with Plaid, and why an interrupted or duplicated run
cannot lose or double-count data.

## Design

The application connects exactly one Plaid Item (handoff §2). Ingestion is
the single code path in this repository permitted to write raw `plaid.*`
rows (CLAUDE.md, handoff §4.1) — implemented in
[`src/finance_app/plaid/sync.py`](../src/finance_app/plaid/sync.py) and the
repositories it calls in `src/finance_app/db/repositories/`.

```text
finance sync / finance-sync.timer
    -> run_daily_sync()
    -> build_client(Settings)          # src/finance_app/plaid/client.py
    -> run_sync(client, access_token)
         -> _ensure_item()             # bootstrap plaid.items, once
         -> loop while has_more:
              fetch one page from /transactions/sync
              upsert plaid.accounts from response.accounts
              upsert plaid.transactions for added + modified
              tombstone plaid.transactions for removed
              advance plaid.sync_state.cursor to next_cursor
              (all of the above in one DB transaction)
         -> record ops.sync_runs (success or error)
```

### Item bootstrap

`plaid.items` holds exactly one row. The first sync calls `/item/get` once
to learn the Item's id and institution, and creates that row; every later
sync skips this call entirely — account metadata comes from the `accounts`
field Plaid already includes on every `/transactions/sync` response, so no
extra API call is needed to keep `plaid.accounts` current. See
[`docs/runbooks/plaid-link.md`](runbooks/plaid-link.md) for how the Item's
access token is obtained in the first place.

### Cursor durability

Plaid's own guidance: retry a failed page from the cursor that was current
*before* that page's request, never from a `next_cursor` whose page might
not have been durably processed. This repository enforces that
mechanically rather than relying on caller discipline: for each page, the
account/transaction row writes and the `plaid.sync_state.cursor` advance
happen inside **one** database transaction (`session_scope()` in
`plaid/sync.py`). The cursor therefore only ever points past data that is
already committed.

Consequences:

- **A process killed mid-page** loses nothing — the next run re-reads the
  last committed cursor and re-fetches/re-processes that exact page.
- **A page that fails to commit** (e.g. an unexpected constraint violation)
  leaves the cursor exactly where it was before that page; a subsequent
  sync resumes there rather than skipping the failed page or replaying an
  already-committed one.
- **Re-processing a page is safe** because every write in it is idempotent:
  - `transactions.upsert` is keyed on `plaid_transaction_id` — insert or
    update in place, never a duplicate row;
  - `transactions.mark_removed` sets `removed_at`; it is a tombstone, never
    a hard `DELETE`, so provenance is never destroyed (handoff §4.1);
  - `accounts.upsert_account` is keyed on `plaid_account_id`.

This is proven by
[`tests/integration/test_plaid_sync.py`](../tests/integration/test_plaid_sync.py),
in particular
`test_a_failed_page_leaves_the_cursor_at_the_last_good_page_and_the_retry_resumes_there`.

### Retries

`/transactions/sync` calls are retried up to 3 attempts with linear backoff
for transient Plaid errors (`RATE_LIMIT_EXCEEDED`, `API_ERROR`). Any other
`error_type` (e.g. `ITEM_LOGIN_REQUIRED`) fails immediately without
retrying — retrying a permission/auth failure just wastes calls and delays
the operator-visible error. Every retry re-requests the *same* cursor, so a
retry cannot skip data even if an earlier attempt partially succeeded on
Plaid's side. See `_sync_page_with_retry` in `plaid/sync.py`.

### Audit trail

Every `run_sync()` invocation writes one `ops.sync_runs` row (`run_type`:
`"scheduled"` | `"webhook"` | `"manual"`) with added/modified/removed
counts, the last Plaid `request_id`, and success/error status — read by
`finops` (handoff §10) and never holding financial payloads. `finance sync`
is `run_type="manual"`; `run_daily_sync()`, the systemd-timer entry point,
is `run_type="scheduled"`.

### Known limitation

If Plaid ever reports a `removed` event for a transaction id this database
has not seen yet (out-of-order delivery across a restart boundary), the
tombstone is a silent no-op — the id is not remembered as pre-removed, so a
later `added`/`modified` for that id would insert it live. This mirrors the
existing repository-layer behavior documented in
`tests/integration/test_transaction_repository_adversarial.py` and has not
been observed to occur within a single cursor stream in practice; revisit
if reconciliation ever surfaces it.

## Not yet built

- The daily `finance-sync.timer` (Milestone 7 — `finance sync` exists and
  is systemd-timer-ready today, it just isn't wired to a timer unit yet).
- The `SYNC_UPDATES_AVAILABLE` webhook (Milestone 8).
- `finops sync-status`, reading `ops.sync_runs`/`plaid.sync_state` through
  the read-only `finance_observer` role.
