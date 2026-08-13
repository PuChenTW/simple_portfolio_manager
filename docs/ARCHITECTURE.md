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

## Container build ordering

The `Dockerfile` installs dependencies from `pyproject.toml` and `uv.lock` alone, before `src/` is
copied, so a source edit rebuilds in about a second instead of resyncing every dependency. Keep
`COPY src` below that step — moving it above ties the whole dependency install to every source
change.

## Sub-path deployment

The app has no URL-prefix setting, and must not grow one. A relative `<base href="./">` in
`static/index.html` is the entire mechanism: the browser resolves it against the document's own
URL, so assets load from `/static/…` at the domain root and `/portfolio/static/…` under a proxy
that mounts the app at `/portfolio`, without the server knowing which happened.

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
deployments. `test_api.py` asserts the `<base href>` stays `./`.
