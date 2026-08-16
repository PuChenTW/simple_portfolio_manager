# 02 — Post a cash-move transaction

**What to build:** On an account's Transactions tab, a "Record transaction" button opens a panel
above the ledger. The panel offers the four cash-move types — `deposit`, `withdrawal`,
`transfer_in`, `transfer_out` — with an amount and a date. Submitting posts the event, and the
new row appears in the ledger below.

This is the thinnest complete path through the feature, and it carries the spine every later
ticket extends. Get these right here and 03–05 are field work:

- The `api.recordTransaction` client method, wrapping the existing `record_transaction`
  operation. No API change, no baseline change.
- **Amounts are strings on the wire.** `inputmode="decimal"` text inputs, sent as JSON strings.
  A float round-trip puts `0.30000000000000004` in the ledger.
- **One `request_id` per form session**, minted when the panel opens, re-minted only after a
  success. A double-click then matches the fingerprint server-side and returns the same event
  instead of posting twice.
- **The date binds to `occurred_at`, not `trade_date`.** `occurred_at` is the only date replay
  reads. Date only, no time. Defaults to today, then sticks to the last submitted value.
- After a success: reload page 0 of the ledger, keep the panel open and cleared with type and
  date preserved, return focus to the amount field, and clear the cached `summary` so it
  refetches on next visit. Leave `performance` alone.
- **If the posted date is before today**, show a line saying snapshots from that date are now out
  of date, linking to the Snapshots panel. Do not rebuild automatically. Without this the user
  gets a correct ledger and a wrong performance chart with nothing connecting them.

Error sentences for `insufficient_cash` (at the amount field) and `idempotency_conflict`. Written
for someone mid-entry with a statement in hand, not for an agent parsing JSON — `AccountSettings`
sets the precedent with `portfolio_name_exists`.

See `../spec.md` for the reasoning behind each of these.

**Blocked by:** 01 — Install a frontend test runner.

**Status:** resolved

- [x] A cash account can record a deposit and a withdrawal end to end, and both appear in the
      ledger without a page reload
- [x] A liability account can record the same, and a drawdown taking cash negative is accepted
- [x] Amounts leave the browser as JSON strings
- [x] Double-clicking submit posts exactly one event
- [x] The posted event's `occurred_at` matches the date entered, verified in the ledger row and
      by replay, not by the form's own display
- [x] A back-dated post shows the stale-snapshot notice; a post dated today does not
- [x] `insufficient_cash` renders as a written sentence at the amount field
- [x] Unit tests cover the type-to-shape mapping
- [x] Verified with Playwright at 375, 768, and 1280 against a scratch `PORTFOLIO_DB_PATH` on a
      non-default port; single column below ~640px

## Comments

Implemented in `ea4bb98`. Every box above was verified against a
scratch `PORTFOLIO_DB_PATH` on port 8011 with Playwright at widths 375, 768, and 1280;
the running Compose stack was left untouched.

The date binding was the one thing verification caught that inspection did not. The first
implementation sent `new Date(occurredOn).toISOString()`, which re-serializes local midnight
in UTC: a date entered as 2026-08-16 stored as `2026-08-15T16:00:00`. The form still showed
the 16th while `replay.py` read the 15th. It now sends the naive local datetime.
