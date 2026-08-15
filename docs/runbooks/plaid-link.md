# Runbook: obtaining and rotating the Plaid access token

This application connects exactly one Plaid Item (handoff §2, §6.5). This
is the owner-facing procedure for obtaining that Item's access token in
Sandbox (development) and production, and for rotating it later. It is a
one-time or occasional manual step, not a service — there is no permanent
Link web UI running anywhere in this stack.

The Claude Code engineering environment never performs this procedure and
never sees the resulting token: it is Plaid Sandbox/production credential
material, out of scope for the engineering agent per CLAUDE.md and
`docs/security-model.md`.

## Development (Plaid Sandbox)

Sandbox lets you skip the Link UI entirely and mint a token for a fake
institution directly against the Plaid API. Run this from a Python shell
with `PLAID_CLIENT_ID`/`PLAID_SECRET` (Sandbox keys) set, using the same
client construction as `finance_app.plaid.client.build_client`:

```python
from finance_app.plaid.client import build_client
from finance_app.config.settings import get_settings
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.products import Products

client = build_client(get_settings())

public_token = client.sandbox_public_token_create(
    SandboxPublicTokenCreateRequest(
        institution_id="ins_109508",  # Plaid's "First Platypus Bank" test institution
        initial_products=[Products("transactions")],
    )
).public_token

access_token = client.item_public_token_exchange(
    ItemPublicTokenExchangeRequest(public_token=public_token)
).access_token

print(access_token)
```

Put the printed value in `.env` as `PLAID_ACCESS_TOKEN` (never commit it —
`.env` is gitignored). `finance sync` will bootstrap `plaid.items` and
`plaid.accounts` from it on first run.

## Production

Sandbox's shortcut above does not exist against real institutions — Plaid
requires the actual Link UI/SDK flow so the end user (the owner, in this
single-user application) authenticates with their bank interactively.
Because this is a headless single-user application, the smallest secure
flow is a **one-time, disposable** local script, run by the owner directly
against production Plaid credentials — never through the Claude Code
engineering environment, never on the production VPS as a long-running
service:

1. The owner runs a small local script (not checked into this repository
   as a running service) that calls `link_token_create` and serves Plaid
   Link — Plaid's own
   [Quickstart](https://plaid.com/docs/quickstart/) is the reference
   implementation — on `localhost` only, for the duration of the link.
2. Complete Link in the browser against the owner's real bank.
3. Exchange the resulting `public_token` for an `access_token` via
   `item_public_token_exchange`, exactly as in the Sandbox example above.
4. Store the `access_token` as a systemd encrypted credential on the VPS
   (`docs/security-model.md` invariant 4) — never in a plaintext `.env` in
   production, never committed.
5. Shut down and delete the local script/environment used for step 1-3. It
   held a production Plaid secret; treat it as sensitive for its entire
   lifetime and confirm no copy of the access token remains outside the
   systemd credential store.

## Rotation / re-authentication

If Plaid reports `ITEM_LOGIN_REQUIRED` (surfaced via `finance sync`'s
non-retryable failure path — see `docs/plaid-sync.md`), the Item needs
Link's "update mode": repeat the production procedure above, passing the
existing `access_token` into `link_token_create`'s `access_token` field
instead of creating a new Item. Replace the stored credential in place; the
underlying `plaid.items` row and all historical `plaid.transactions` rows
are unaffected — only the access token changes.
