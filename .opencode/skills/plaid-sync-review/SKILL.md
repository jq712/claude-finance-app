---
name: plaid-sync-review
description: Attack checklist for any change touching Plaid Transactions Sync ingestion or cursor semantics (src/finance_app/plaid/**) — added/modified/removed handling, pagination, interruption safety, idempotent re-sync, and the webhook/cron sync race. Use before or while reviewing a Plaid sync change.
---

# Plaid sync change review

Consolidates the `qa-adversarial` agent's Plaid ingestion attack list, the `database` agent's
sync-semantics notes, and ADR-003/ADR-012. Any change to sync semantics is Class B (AGENTS.md's
Risk classes) — implementation, then independent `qa-adversarial` + `security-reviewer` review
(separate fresh-context invocations, launched in parallel), then the full ADR-017 gate.

## The two questions that matter

**Can an interruption lose an update?** **Can a re-run duplicate a row?**

Every item below is a way of trying to answer "yes" to one of those. If you can't construct a
fixture that answers both "no" for the change under review, it isn't done.

## Required transaction-boundary property

The durable cursor must never advance ahead of persisted writes. Concretely: within one sync
page, the row writes and the cursor update happen in the same transaction (or an equivalent
all-or-nothing unit), so a crash between "wrote the rows" and "advanced the cursor" is either
fully applied or fully not — never half. Reason about this from the actual transaction
boundaries in the code, not from what looks safe.

## Attack checklist

- Initial cursor state (first sync ever, no prior cursor).
- Empty/no-update response from Plaid.
- `added` transactions.
- `modified` transactions (including a transaction Plaid modifies after a category override was
  already applied to it on our side — does the override survive, or does it get silently
  clobbered by the raw update, or does provenance get destroyed either way?).
- `removed` transactions.
- Multi-page pagination — does state persist correctly across pages?
- A transaction mutating **during** pagination (Plaid returns it in page 1, then modifies/removes
  it before page 2 is fetched).
- Duplicate page or event delivery (the same page or webhook fires twice).
- `pending` → `posted` replacement (same real-world transaction, different `transaction_id` or an
  update to the same one, depending on Plaid's actual behavior — verify current Plaid docs rather
  than assuming).
- Process killed mid-sync (partial page write, no cursor advance — does resume from the
  last-good cursor reprocess correctly without duplicating?).
- Network error and retry mid-page.
- Database rollback mid-run.
- The same sync run executed twice concurrently (duplicate systemd trigger, or a webhook-
  triggered sync racing the daily cron sync for the same item — **these must share one locking
  strategy**, not two independent ones that can both believe they hold the lock).
- Re-authentication and item-health failures (expired/revoked access token, `ITEM_LOGIN_REQUIRED`
  and similar Plaid error codes).

## Idempotency

Natural keys (`transaction_id`, `account_id`, `item_id`) must be unique-constrained (see
`.opencode/skills/safe-migration/SKILL.md`) so a full or partial re-sync of already-seen data
cannot duplicate rows at the database level, independent of application-layer de-duplication
logic getting it right.

## Webhook interaction (Milestone 8+)

Once a webhook exists, a `SYNC_UPDATES_AVAILABLE` event and the daily reconciliation cron both
end up calling the same sync path for the same item. Verify: they use the same concurrency
lock; a webhook firing mid-cron-sync (or vice versa) cannot cause a double-processed page or a
lost cursor advance; and daily reconciliation is retained regardless of webhook health (ADR-012
— the webhook is an enhancement, not a replacement for the safety net).

## Before opening the PR

Run `pre-merge-review` in addition to the required `qa-adversarial`/`security-reviewer` gate —
fresh-context correctness review is additive here, not a substitute for the Class B specialist
review this class of change already requires.
