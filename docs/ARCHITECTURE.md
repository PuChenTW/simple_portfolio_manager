# Architecture Notes

Why the non-obvious parts of this codebase are shaped the way they are. `AGENTS.md` carries the
rules; this file carries the reasoning. Read the relevant section before changing a subsystem —
several of these designs look like overhead until you know what they prevent.

## The journal

`journal.py` defines the event and leg vocabulary and the balance validator, plus the
flow-classification rules every layer shares.

`effective_type` resolves a `reversal` to the type of the event it undoes. A `reversal` carries no
economic meaning of its own, so classifying it literally reports a fully recorded correction as an
unresolved one. The sign needs no special handling: a reversal's legs are already inverted, so a
reversed deposit lands in the same category with the opposite sign.

`postings.py` performs atomic posting and reversal, and also serves page reads. `legs_for_events`
and `reversed_types_for` each resolve a whole page in one query, so listing never costs a query
per row.

`corporate_actions.py` records, previews, and applies issuer events.

## Cash accounts and transfers

A cash account is a `Portfolio` with `kind = "cash"` — the same journal, replay, valuation, and
performance machinery, minus the ability to hold a position. That reuse is possible because
`cash_balances` has no currency column: a portfolio's `base_currency` *is* its cash currency, so a
bank balance was always expressible as a portfolio that never bought anything. What the kind adds
is a refusal. `reject_security_activity` lives in `postings.py` rather than the route layer
because `corporate_actions.py` posts through `_persist` and `_apply_projections` directly, and a
guard in the API would let a split into a savings account through the side door.

`transfers.py` moves cash between two portfolios. A journal event belongs to exactly one
portfolio, so a transfer is **two** events sharing a `transfer_id`, written in one transaction.
The alternative — two unlinked events, one per side — is what the codebase had, and it can leave
money in neither account when the second post fails, with nothing recording that the two were the
same movement.

Each half balances on its own, in its own portfolio's currency. This is why a cross-currency
transfer is not one event holding two currencies: `validate_balance` nets every leg into a single
functional currency, so such an event balances only after converting one side, and a residual that
is zero in one currency's terms is an unbalanced event wearing an exchange rate. Keeping the halves
separate is also what left `replay.py` untouched — each portfolio still sees one ordinary event of
a type it already knew.

The executed rate is stored in the counter-leg's `leg_metadata`, never in `Leg.fx_rate`.
`functional_amount` multiplies by that field unconditionally, and both legs are already in their
event's own currency, so a rate there would scale one side of a balanced pair. The rate is the
user's to supply: a market rate differs from the one a bank actually gave, and the gap would post
as cash from nowhere.

`transfer_id` is deliberately not a foreign key. The pair spans two portfolios, so a constraint
would either block deleting one side or corrupt the survivor's link; a dangling id is honest,
because the counterparty record really is gone. `transfer_role` is stored rather than derived,
since the reversal of a transfer-out carries an inflow sign and the cash sign alone cannot say
which side an event belongs to.

Transfers stay **external** in `classify_flow`, unchanged. At the single-portfolio level that is
correct — the money genuinely crossed that book's boundary, and TWR must neutralize it or moving
cash into a broker would read as a return. Netting a pair is a group-level question, and
`consolidation.py` computes no group-level flow or return today, so there is nothing to distort.
The `transfer_id` and the counterparty in the leg metadata make the pairs identifiable whenever a
group-level return is built; the correction belongs with that feature, not before it.

## Liability accounts

A loan is `kind = "liability"`: the same book again, with the balance meaning what is owed. It
needed almost no new machinery, because the ledger was always signed. Cash legs carry a direction,
`replay.py` folds them into a plain sum, `valuation.py:246` adds `securities + cash` with no clamp,
and `consolidation.py` sums members the same way — so a negative balance already flowed through
every total correctly. What was missing was permission for it to exist.

That permission is `_owes_by_design` in `postings.py`, deliberately **not** folded into the
existing `allow_negative_cash` parameter. The two answer different questions: `allow_negative_cash`
is a caller waiving the overdraft check for one posting, while the kind is a standing property of
the account. Collapsing them would let a waiver anywhere read as a liability everywhere. It is
looked up inside `_apply_projections` rather than passed in because all four callers would
otherwise have to thread a kind they have no other use for — and because putting it there is what
let `transfers.py` keep its hardcoded `allow_negative_cash=False`: a drawdown is an ordinary
transfer whose loan side is permitted to go negative by the account, not by the transfer.

`reject_security_activity` tests set membership rather than equality against `CASH`. An equality
test would have let securities into a loan account through the kind added next, and the guard
would have failed open — silently, since nothing else refuses a position.

**Interest charged is a `fee`, not `interest`.** `build_legs` routes `INTEREST` to `_income_legs`,
which credits cash: it is interest *received*, the canonical cash-account income event. A loan's
interest moves the other way. `FEE` already produces a negative cash leg and already classifies
as internal, so it is the correct existing vocabulary rather than a near-miss. Adding an
`INTEREST_EXPENSE` type would touch the enum, the flow sets, `build_legs`, the taxonomy resource,
and the MCP error codes to buy a distinction between interest and an origination fee that nothing
currently computes with.

**Performance refuses rather than reports.** `calculate_performance` returns no TWR or XIRR for a
liability book, with the reason attached. A rate of return divides a gain by the capital that
earned it, and a debt is not capital at work. This is not a limitation being papered over: run
through Modified Dietz, a base of −1,000,000 recovering to −950,000 is a gain of 50,000 divided by
a negative denominator, reported as **−5%**. Repaying a loan would read as a loss. The denominator
guard in `_daily_returns` was widened from `== ZERO` to `<= ZERO` for the same reason, so any book
that reaches a non-positive base — a margin overdraft, say — yields no number instead of an
inverted one.

The consolidated summary splits its total into `assets_value`, `liabilities_value`, and
`net_value`. `net_value` **is** `total_value`, unchanged; the split exists because one net figure
cannot tell 5M in cash from 15M against a 10M loan. Liabilities stay negative rather than being
flipped to a magnitude, so the three reconcile by addition and a reader cannot add where they
should subtract. Debt is bucketed per currency *before* conversion and reuses the rates already
resolved for the same balances as cash, so the split can never disagree with the total it came
from, and an unconvertible pair stays excluded from both.

One known rough edge, deliberately left: allocation weights divide by total value
(`services.py`, `consolidation.py`), so a group whose debts exceed its assets produces negative
weights. `safeWidth` in the dashboard rejects them and renders a zero-width bar, so nothing
breaks visibly. Changing the denominator to gross assets would alter every existing account's
existing numbers, which is a separate decision from recording a debt.

## Historical valuation

`replay.py` rebuilds positions, cash, and flow totals at any cutoff by folding journal legs. It is
the only correct source of past state — `positions` and `cash_balances` describe the present.

`valuation.py` prices a replayed state with history bounded by the valuation date and stores it as
a snapshot. It also holds the re-runnable range rebuild behind `portfolio-admin rebuild-snapshots`.

`performance.py` computes TWR and XIRR from stored snapshots and journal flows, and reports the
coverage behind them: a gap, a partial valuation, or an event whose cash flow cannot be classified
each makes a return unreliable, and the number is worthless without that context.

`fx.py` resolves point-in-time exchange rates — direct, inverted, or crossed — and stores every
observation so a conversion can be audited. `consolidation.py` groups portfolios and expresses
their holdings in one reporting currency, keeping each local figure beside its converted one.

## The market-data cache

`cache.py` wraps the `MarketProvider` protocol as a Redis layer, so no consumer knows it exists.

Daily bars are stored one calendar month per key. Callers ask for ranges that start and end
mid-month, and a bucket holding only the requested slice would leave a gap the cache itself
invented — so a fetch widens to the month boundary and only whole months are stored.

The month containing today expires in minutes because its last bar is still moving. Earlier months
last far longer because a closed session's OHLC is a fact.

Every Redis failure degrades to the provider: a cache that can break a request is worse than no
cache. Leaving `PORTFOLIO_REDIS_URL` unset disables the layer entirely.

That "fact" has one exception, and it is why `clear_market_cache` exists. Auto-adjusted bars are
restated by the provider after a split or dividend, which no rule here can detect. Recording such
an action logs a warning naming the ticker rather than expiring entries on a guess about when the
restatement happened. Clearing is always safe, since the cache holds nothing that cannot be
fetched again.

## Quote freshness

`sessions.py` answers when a live quote can next change, and `MarketService` caches it for exactly
that long. The two layers are unrelated: this one governs the single current quote in `quote_cache`,
while `cache.py` above governs daily history bars in Redis.

A fixed TTL has to be wrong in one direction. Five minutes is right during a session and pointless
after the close, when the price cannot move until the next open — an overnight dashboard refetches
the same number every five minutes for sixteen hours. So the TTL is derived from the calendar
rather than configured: open markets keep `PORTFOLIO_QUOTE_TTL_SECONDS`, closed ones hold until
the next open. Nothing needs tuning because nothing was guessed.

Two cases must not take the long TTL, and both fail quietly rather than loudly if they do. Crypto
never closes, so caching it until a notional open would freeze the price indefinitely; it has no
session entry and always uses the short window. A market whose hours are unknown gets the same
treatment for the same reason — inventing a close for it would freeze a price that is still
moving. The long TTL is also floored at the short one, so a quote fetched a minute before the bell
is never cached for less than it would have been mid-session.

Weekends count as closed; market holidays deliberately do not. A holiday expires the quote at the
notional open, where the provider returns the same unchanged close and it is cached again until
the following open — one wasted request per holiday, and never a wrong price. A hand-maintained
holiday calendar buys that request back and costs a silent failure mode instead: once it drifts
out of date it marks a real trading day as closed and serves yesterday's price for a full session.

A closed-market cache hit is **not** stale, and must never set the flag or a warning. The price is
genuinely current — it is the last trade, and there will not be another until the open. Marking it
otherwise would put a warning nobody can act on on every overnight read, which is exactly the
failure `AGENTS.md` warns about: readers learn to ignore warnings, including the ones that matter.
`stale` stays reserved for its real meaning, a refresh that failed and fell back to a cached quote.

## The MCP surface

`mcp_server.py` is a thin HTTP client that wraps each API endpoint as a tool. It also holds the
MCP prompts and resources, which do different jobs: prompts carry multi-step workflows and the
mistakes their ordering prevents, resources carry the vocabulary and conventions a call needs
before any tool is chosen.

Generate a resource from the enum it documents rather than restating it, so the two cannot drift.
`test_mcp_server.py` enforces this, along with the rule that no prompt may name a nonexistent
tool — a workflow referencing a removed tool fails mid-task, which is worse than having no
workflow at all.

## API version history

Each bump to `tests/legacy_api_baseline.json` was a deliberate decision, not a regenerated diff.

- **0.2.0** — breaking. Removed the pre-journal `record_trade` and `record_cash_transaction`
  ledgers, whose position and cash writes were independent and could disagree.
  `record_transaction` is now the only write path.
- **0.3.0** — additive. `include_legs` on `list_journal_events` returns each event's legs inline,
  so reading a day of activity costs one request rather than one per event.
- **0.4.0** — additive. `clear_market_cache` drops cached price history for one ticker, needed
  because the Redis layer cannot tell when a provider restates its auto-adjusted bars.
- **0.5.0** — cash accounts and transfers. Five operations, five models, five tools, and the one
  frozen shape that moved: `PortfolioRead` gained `kind` and `institution`. `list_portfolios` is
  how a caller discovers portfolios, and without `kind` on that response it cannot tell a bank
  account from a brokerage account without a second request each — a worse contract than the
  break. Both fields default, so a client that ignores them reads what it read before.
- **0.6.0** — liability accounts. Two operations, one model, two tools, and three fields added to
  `ConsolidatedSummaryRead`. The fields are what forced the bump: `assets_value`,
  `liabilities_value`, and `net_value` split a number that was already correct, because
  `total_value` alone cannot say whether a net figure is cash or the remainder after a loan.
  `PortfolioKind` also gained `liability`, which the baseline does **not** freeze — it captures a
  `$ref`, not the members — so a client switching on `kind` should treat an unknown value as a
  book it cannot interpret rather than defaulting it to `investment`.
- **0.7.0** — portfolio and group renaming. No frozen shape moved.
- **0.8.0** — additive. `asset_class` and `asset_class_provenance` on
  `ConsolidatedPositionRead`, so a group's exposure can be read by what its holdings *are*
  rather than only by ticker. Both default to `unclassified`; nothing else moved.

  They sit on the summary because that is where every holding is already listed together —
  answering the same question through `get_instrument_profile` costs one request per holding and
  still needs a client-side join against values the summary just returned. The provenance
  travels with the value because a `derived` equity read off a provider's `quoteType` and a
  verified `manual_override` are not equally trustworthy, and a reader that cannot tell them
  apart cannot know which numbers rest on a provider's guess.

  Nothing is defaulted to a plausible exposure. A provider states a fund's wrapper and never its
  contents, so every ETF arrives `unclassified` and stays that way until someone resolves it;
  reading them as equity would have made the allocation view look complete while misfiling gold
  and bond funds where nothing downstream could detect it.

## Container build ordering

The `Dockerfile` installs dependencies from `pyproject.toml` and `uv.lock` alone, before `src/` is
copied, so a source edit rebuilds in about a second instead of resyncing every dependency. Keep
`COPY src` below that step — moving it above ties the whole dependency install to every source
change.

## The dashboard mount

The dashboard is a Svelte build from `frontend/`, and `src/portfolio_manager/static/` is now
entirely its output — gitignored, produced by `bun run build`, and baked into the image by the
`frontend` Docker stage. It replaced a hand-written HTML page that was served by a route reading
a file at import time. Two things about the replacement are load-bearing.

**The mount is the last statement in `api.py`.** A Starlette mount matches by prefix, so one at
`/` shadows every route declared after it. Nothing in the file guards this — a new endpoint
appended below the mount would return the dashboard's HTML instead, with no error anywhere. The
comment there says so, because the failure is invisible in review and invisible in the tests that
target the endpoints declared above it.

**The mount is conditional on the directory existing.** Mounting a missing directory raises at
import time, so a checkout that has never run `bun run build` would fail to start the API at all.
Making it conditional keeps the two artifacts independent: the service is an API that happens to
ship a dashboard, and a build without one still answers every endpoint. `test_api.py` skips its
page assertions when the build is absent for the same reason.

There is no `/v2`. The Svelte app was served there while the old page held `/`, and promoting it
moved it to the root rather than keeping both. One dashboard, one URL — a redirect would have
been a second name for the same page, kept alive for bookmarks that only ever existed on one
developer's machine.

## Sub-path deployment

The app has no URL-prefix setting, and must not grow one. Relative asset URLs are the entire
mechanism: Vite builds the dashboard with `base: './'`, so the browser resolves every path
against the document's own URL. Assets load from `/assets/…` at the domain root and
`/portfolio/assets/…` under a proxy that mounts the app at `/portfolio`, without the server
knowing which happened. The app's own routes are hashes for the same reason — a fragment never
reaches the server, so it needs no base either.

That is not a stylistic preference. Tailscale Serve's `--set-path` **strips the prefix before
forwarding** and sends no `X-Forwarded-Prefix` — measured with a header-probe server behind a
temporary `--set-path=/hdrprobe` mapping, where a request to `/hdrprobe/deep/path.js` arrived as
`PATH: /deep/path.js`, carrying only `Host`, `User-Agent`, `Accept`, `Accept-Encoding`, the
`Tailscale-*` identity headers, and `X-Forwarded-For` / `-Host` / `-Proto`. The README claimed
the opposite for several releases; it was wrong.

So a proxied request and a direct one are byte-identical at the app. No server-side logic can
distinguish them, which means any per-deployment prefix config necessarily breaks whichever
deployment it was not set for — the earlier `PORTFOLIO_URL_PREFIX` baked `<base href="/portfolio/">`
into every response and left direct access on `localhost:8001` serving 404s for its own assets.
Its companion `StripPrefixMiddleware` never ran at all: it stripped a prefix that never arrived.

The corollary for anyone editing the dashboard: keep every asset and API path relative, and never
hardcode a leading `/`. One absolute path silently reintroduces the bug for one of the two
deployments. `test_api.py` asserts every asset path the built page emits starts with `./`, and
that each one resolves — a page whose bundle 404s still answers 200.
