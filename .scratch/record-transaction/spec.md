# Record transactions from the dashboard

Add a posting path to the dashboard. Today the journal is read-only there: the Transactions tab
pages and expands events, and the only writes the whole app makes are `updatePortfolio`,
`deletePortfolio`, `rebuildSnapshots`, and `setAssetClass`.

## Scope

Frontend only. `record_transaction` and `reverse_transaction` both exist and are complete, so
`tests/legacy_api_baseline.json` is untouched and there is no version bump. Two new client
methods wrap existing operations.

Corporate actions are out of scope. `split`, `merger`, and `spinoff` post through
`record_corporate_action`, which has a preview/apply flow of its own.

## Placement

A panel inside the Transactions tab, above the ledger, opened by a "Record transaction" button.
The table below is the confirmation that the posting landed, so the form and its evidence share
one viewport. A modal would hide the table the user needs to check.

## Three shapes, one form

The type picker comes first, because the type determines every other field. The fields below it
swap between three shapes:

| Shape | Types | Fields |
|---|---|---|
| Trade | `buy`, `sell` | ticker, quantity, unit price, fee, tax |
| Cash move | `deposit`, `withdrawal`, `transfer_in`, `transfer_out` | amount |
| Income / cost | `dividend`, `interest`, `fee`, `tax` | amount, optional ticker, tax |

The picker filters by `portfolio.kind`. Cash and liability books offer only the cash-move and
income/cost types, and the ticker field never renders for them — `reject_security_activity`
refuses a ticker on a positionless book, and a form that offers what the server refuses is lying
about what it accepts.

## The date field

**One date field, bound to `occurred_at`.** This is the decision most easily gotten wrong by
reading the schema instead of the code. `replay.py:348` filters and orders on `occurred_at`
alone; `trade_date` is display metadata that drives no valuation. A form collecting "date" into
`trade_date` posts an event that reads as June in the table and replays as today — invariant 5
broken invisibly.

Date only, no time. `replay.py:350` breaks same-day ties on `created_at`, which is entry order,
and for manual entry that is as good an order as any.

Defaults to today, then sticks to the last submitted value for the next entry in the session.
Statement entry is a burst of transactions sharing one date, and re-typing it each time is where
a wrong date comes from.

## Idempotency

One `request_id` per form session, minted when the form opens and re-minted only after a
success. A double-click hits `postings.py:471`, matches the fingerprint, and returns the existing
event — one posting, not two. A failed submit retries under the same key.

This is deliberately unlike `setAssetClass`, which mints per call because two edits to one ticker
are two mutations. Here the repeat click is the hazard, and idempotency is the only thing between
it and a duplicate posting.

## Precision

Every amount is a string on the wire. Text inputs with `inputmode="decimal"`, sent as JSON
strings, which Pydantic parses to `Decimal` exactly. A float round-trip puts
`0.30000000000000004` in the ledger, which is exactly what the repo's `Decimal` rule exists to
prevent.

The cash-effect preview may use floats, since it is a sanity check a human reads, formatted to
the currency's precision.

## Ticker resolution

On blur, resolve the ticker and show the instrument name beside the field. Two warnings, both
non-blocking — the server stays the authority:

- Unresolvable: the symbol may be delisted, which `_resolve_or_fetch` cannot seed.
- Currency differs from the account's base currency: "AAPL trades in USD; this account is TWD."

The second converts a post-submit `currency_mismatch` 422 into a pre-submit fact, using a request
that is already being made.

## Errors

Nine codes reach this form. Each maps to a written sentence shown at the field it concerns —
`insufficient_cash` and `insufficient_position` at amount or quantity, `currency_mismatch` and
`market_data_unavailable` at the ticker. `AccountSettings` sets this precedent with
`portfolio_name_exists`. The API's own messages are written for agents parsing JSON, not for
someone mid-entry with a statement in hand.

## After a success

Reload page 0 of the ledger; the new row appearing is the confirmation. The form stays open and
clears, with type and date preserved and focus returning to the first field of the shape.
Collapsing it would cost a click per transaction during a burst.

Clear `summary` so it refetches on next visit, rather than paying a quote per ticker for a tab
nobody is looking at. Leave `performance` alone: its snapshots are genuinely stale until a
rebuild, and refetching would redisplay the same wrong numbers with more confidence.

**If `occurred_at` is before today**, say so: "Snapshots from 2026-06-01 are now out of date,"
linking to the Snapshots panel. Do not auto-rebuild — a force rebuild is what `AccountSnapshots`
deliberately gates behind a second click naming how many snapshots it replaces. Without this
notice the user gets a correct ledger and a wrong performance chart with nothing connecting them.

## Reversal

Ships alongside. Posting without undo is half a feature, and the first thing a user does after a
bad entry is look for it.

The confirmation must state that the reversal lands **today**, not on the original date. A dated
mistake needs a dated adjustment instead, and this is where that trap gets caught.

## What this design deliberately does not do

- **No full leg preview.** It would duplicate `build_legs` in TypeScript, and the copy would
  drift. One computed cash-effect line is the whole preview.
- **No client-side blocking.** Every warning is advisory; every refusal is the server's.

## Verification

- Vitest over the cash-effect formula and the type-to-shape mapping. The preview is a second
  implementation of `build_legs`' arithmetic — the one piece inspection will not catch.
- `bun run check` and `bun run build`, then Playwright against a scratch `PORTFOLIO_DB_PATH` on a
  non-default port. The running Compose stack holds real data and must stay untouched.
- Widths 375, 768, and 1280, stated in the PR. Single column below ~640px.
