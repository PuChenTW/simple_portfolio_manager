# 03 — Post a trade

**What to build:** `buy` and `sell` appear in the type picker on investment accounts. Choosing
one swaps the panel to the trade shape: ticker, quantity, unit price, fee, and tax. Submitting
posts the trade and it appears in the ledger.

Two things this ticket adds beyond the fields.

**The live cash-effect line.** Above the submit button, a single line reading
`Cash effect: −1,234.56 USD`, recomputed as the user types. For a buy that is
`−(quantity × price + fee + tax)`; for a sell, `quantity × price − fee − tax`. It catches a
misplaced decimal before it reaches a ledger where the only correction is a reversal that lands
on today's date.

This line re-implements `build_legs`' arithmetic in TypeScript, which makes it the one piece of
this feature that inspection will not catch when it is wrong. Unit-test it against the cases
`build_legs` handles, including the fee and tax capitalization — costs are added to a buy's cash
outflow and subtracted from a sell's proceeds, and getting that sign backwards is the plausible
mistake. Do not extend it into a full leg preview: that would duplicate more of `build_legs`, and
the copy would drift.

**Ticker resolution on blur.** Resolve the symbol and show the instrument name beside the field
as confirmation — a typo that resolves to a real but wrong company is the failure the server
cannot catch. Two warnings, both non-blocking:

- Unresolvable. The symbol may be delisted, which the server cannot seed. The user still needs
  the server's actual error, so do not block submit on it.
- Resolved currency differs from the account's base currency: "AAPL trades in USD; this account
  is TWD." This turns a post-submit `currency_mismatch` 422 into a pre-submit fact, using a
  request that is already being made.

Error sentences for `insufficient_position` (at the quantity field), `currency_mismatch`, and
`market_data_unavailable` (both at the ticker field).

**Blocked by:** 02 — Post a cash-move transaction.

**Status:** resolved

- [x] An investment account can record a buy and a sell end to end
- [x] Fee and tax capitalize into the cash effect with the correct sign for both directions
- [x] The cash-effect line matches the settlement leg the server actually posts, checked against
      a real posting rather than against the formula alone
- [x] Unit tests cover the cash-effect formula, including zero fee, zero tax, and both directions
- [x] A valid ticker shows its instrument name; an unknown one warns without blocking submit
- [x] A ticker whose currency differs from the account's warns before submit
- [x] Selling more than the held quantity renders `insufficient_position` at the quantity field
- [x] Verified with Playwright at 375, 768, and 1280

## Comments

Implemented in `455689f`. Every box above was verified against a
scratch `PORTFOLIO_DB_PATH` on port 8011 with Playwright at widths 375, 768, and 1280;
the running Compose stack was left untouched.
