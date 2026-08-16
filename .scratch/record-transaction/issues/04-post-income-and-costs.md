# 04 — Post income and costs

**What to build:** The third and final shape. `dividend`, `interest`, `fee`, and `tax` appear in
the picker, taking an amount, an optional ticker, and — for income — a withholding tax field.
Income is recorded gross with withholding split out, which is what the server already does; the
form's job is to make that split enterable rather than to compute it.

This ticket completes the picker, and with it the **kind filter**. Cash and liability accounts
offer only the cash-move and income/cost types, and the ticker field never renders for them.
`reject_security_activity` refuses a ticker on a positionless book — including on a fee or
interest event, because a ticker there attaches an instrument to the leg, which is the same
mistake wearing a different name. A form that offers `buy` on a savings account only for the
server to refuse it is lying about what it accepts.

Note the vocabulary trap, which the form should not try to paper over: on a liability account,
**interest charged is a `fee`**, not `interest`. `interest` credits cash and means interest
received. The type list is the server's, and renaming it in the UI would put a second name on a
concept that already has one.

Blocked by 03 rather than 02 because the optional ticker field and its resolution warnings are
the same component that ticket builds.

**Blocked by:** 03 — Post a trade.

**Status:** resolved

- [x] An investment account can record a dividend with withholding tax, and the posted event
      shows gross income with the tax split out
- [x] Interest, fee, and tax post end to end
- [x] A dividend with a ticker attaches the instrument; one without still posts
- [x] A cash account's picker offers no `buy` or `sell`, and renders no ticker field for any type
- [x] A liability account's picker behaves the same way
- [x] Unit tests cover the kind-to-available-types mapping
- [x] Verified with Playwright at 375, 768, and 1280

## Comments

Implemented in `0c2bd0d`. Every box above was verified against a
scratch `PORTFOLIO_DB_PATH` on port 8011 with Playwright at widths 375, 768, and 1280;
the running Compose stack was left untouched.
