# 06 — Reverse a posted transaction

**What to build:** Each un-reversed row in the ledger gains a reverse action. Confirming it posts
a reversal through the existing `reverse_transaction` operation, and both rows — the original,
now tagged `reversed`, and the new reversal — appear in the table.

Posting without undo is half a feature. The first thing a user does after a bad manual entry is
look for the undo, and there is nowhere to look today.

**The confirmation must state that the reversal lands today, not on the original date.** This is
the trap the whole ticket exists to catch: a mistake dated in June is not undone by an event
dated today, because replay reads `occurred_at` and the two dates sit in different periods. A
dated mistake needs a dated adjustment instead. Say that plainly in the confirmation, so someone
reaching for undo learns it before they use it rather than after.

Guard it the way `AccountSettings` guards deletion — a second, deliberate confirmation, not a
dialog nobody reads. A reversal is itself a posting, and correcting one is another posting.

Three refusals to handle:

- `already_reversed` — the row should not offer the action, but the server is the authority.
- `cannot_reverse_a_reversal` — correct a reversal by posting a replacement.
- `reverse_the_transfer_instead` — a transfer half cannot be reversed here, because the pair must
  come undone together. Point the user at the transfer, do not offer a partial undo.

After a success, reload page 0 and clear the cached `summary`, the same way ticket 02 does. If
the reversed event was back-dated, the same stale-snapshot notice applies.

Blocked only by 02 — it needs the ledger-reload plumbing, not the trade or income shapes — so it
can run in parallel with 03–05.

**Blocked by:** 02 — Post a cash-move transaction.

**Status:** resolved

- [x] A posted event can be reversed from the ledger, and both rows show their tags afterwards
- [x] The confirmation states that the reversal is dated today, not on the original event's date
- [x] The action requires a deliberate second confirmation
- [x] An already-reversed event offers no reverse action
- [x] Attempting to reverse a reversal, or a transfer half, renders the server's refusal as a
      written sentence
- [x] Reversing a back-dated event shows the stale-snapshot notice
- [x] Verified with Playwright at 375, 768, and 1280

## Comments

Implemented in `2d182cb`. Every box above was verified against a
scratch `PORTFOLIO_DB_PATH` on port 8011 with Playwright at widths 375, 768, and 1280;
the running Compose stack was left untouched.

One fix outside the ticket: `.visually-hidden` had no `left`, so with no positioned ancestor
it anchored to the initial containing block. Harmless until the new Actions header column put
one at the table's right edge, where it extended the document past the viewport and made the
whole page scroll sideways at 375px. `.scroll` was clipping the table correctly the whole
time; the span sat outside what it clipped.
