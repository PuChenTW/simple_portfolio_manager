# 05 — Advanced fields for statement reconciliation

**What to build:** A collapsed "Advanced" disclosure at the foot of the panel, holding
`settlement_amount` and `source_reference`, on every shape that accepts them.

`settlement_amount` overrides the computed cash when a broker reports an exact figure — FX
rounding, odd fees, a statement that does not quite match the arithmetic. `source_reference` is
the broker confirmation or statement ID, which is what makes a posted event reconcilable against
the document it came from later.

Both exist precisely for entering from a statement, which is the main reason to post by hand at
all. They are collapsed by default because a wrong `settlement_amount` unbalances the event, and
because ordinary entry never needs either.

When `settlement_amount` is supplied on a trade, the cash-effect line from ticket 03 should show
the supplied figure rather than the computed one — the preview's job is to state what will
actually be posted.

Split out from 04 rather than bundled because "advanced fields nobody needs for ordinary entry"
is the scope that quietly expands to fill a context window.

**Blocked by:** 04 — Post income and costs.

**Status:** resolved

- [x] The disclosure is collapsed on open and its fields are omitted from the request when empty
- [x] A trade with an explicit `settlement_amount` posts that exact cash figure
- [x] The cash-effect line reflects a supplied `settlement_amount` instead of the computed value
- [x] `source_reference` round-trips and appears in the ledger row's expanded provenance line
- [x] A `settlement_amount` that unbalances the event surfaces the server's refusal as a written
      sentence at that field
- [x] Verified with Playwright at 375, 768, and 1280

## Comments

Implemented in `3616b80`. Every box above was verified against a
scratch `PORTFOLIO_DB_PATH` on port 8011 with Playwright at widths 375, 768, and 1280;
the running Compose stack was left untouched.
