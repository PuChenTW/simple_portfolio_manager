# Local Portfolio Manager

A local FastAPI service designed for agents that need to track cash, US and Taiwan stocks,
and cryptocurrencies. It keeps an auditable double-entry journal, values holdings on any past
date, measures return, and reports several portfolios together in one currency.

Its governing rule is that a value the service cannot determine is reported as undetermined. A
holding it cannot price is excluded and the snapshot marked partial; a currency pair it cannot
resolve leaves the amount unconverted with a coverage percentage; a classification it cannot
establish stays `unclassified`. Nothing is filled with a plausible default, because an invented
number is indistinguishable from a real one once written.

## Setup

```bash
uv sync
uv run alembic upgrade head
uv run portfolio-manager
```

The server listens on `http://127.0.0.1:8001`. OpenAPI documentation is available at
`http://127.0.0.1:8001/docs`. The service remains bound to loopback only.

The read-only Traditional Chinese dashboard at `http://127.0.0.1:8001/` has four tabs: current
holdings, historical performance (NAV series with TWR and XIRR), data quality (classification
provenance and the journal), and the cross-currency consolidated total. It never changes data.

Set `PORTFOLIO_DB_PATH` to choose the SQLite database file. Quote data is fresh for 300
seconds by default; override it with `PORTFOLIO_QUOTE_TTL_SECONDS`.

## Typical flow

Create a USD portfolio:

```bash
curl -X POST http://127.0.0.1:8001/api/v1/portfolios \
  -H 'content-type: application/json' \
  -d '{"name":"US long term","base_currency":"USD"}'
```

Use the returned portfolio ID to post transactions. `request_id` should be a new UUID or another
client-generated unique value for every logical mutation; retrying the same payload with the same
ID is safe.

```bash
curl -X POST http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/transactions \
  -H 'content-type: application/json' \
  -d '{"request_id":"dep-001","transaction_type":"deposit","amount":"10000"}'

curl -X POST http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/transactions \
  -H 'content-type: application/json' \
  -d '{"request_id":"buy-001","transaction_type":"buy","ticker":"AAPL","quantity":"10","unit_price":"200","fee":"1"}'
```

`record_transaction` posts the security, cash, fee, and tax legs in one database transaction, so
a position can never move without its settlement. It is the only way to record activity, and a
posted event is corrected by `reverse_transaction`, never edited or deleted.

Read the full valuation at `/api/v1/portfolios/PORTFOLIO_ID/summary`. Prices, quantities,
and amounts are JSON decimal strings. All timestamps are UTC RFC 3339 values.

Ticker conventions are `AAPL` for US stocks, `2330.TW` for TWSE, `8069.TWO` for TPEX, and
`BTC-USD` for crypto. A portfolio only accepts assets whose quote currency matches its base
currency; use separate portfolios for TWD and USD holdings, then group them for a combined view.

## Historical valuation and performance

The journal is the source of truth, so any past date can be rebuilt from it. A snapshot replays
the journal to a cutoff and prices the result with history bounded by that same date — never
with today's quote, which would make a backfilled series look like it predicted the market.

```bash
# Build daily snapshots over a range. Safe to re-run: existing dates are skipped, so an
# interrupted run is resumed by repeating the command.
uv run portfolio-admin rebuild-snapshots PORTFOLIO_ID 2026-07-01 2026-07-31

curl 'http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/performance?start_date=2026-07-01&end_date=2026-07-31'
```

Two returns come back because they answer different questions. `twr_percent` removes the effect
of deposits and withdrawals, so it measures the holdings and is what you compare against a
benchmark. `xirr_percent` keeps that effect, so it measures what the investor earned on the
capital actually at risk. Neither is `total_pnl / cost_basis`, which is not a return.

Check `coverage.is_reliable` before quoting either. It is false when the snapshot series has
gaps, contains partial valuations, or holds an event whose cash flow could not be classified —
each biases the result in a direction the service cannot correct.

### Opening an account that already holds cash and stock

A position cannot appear from nothing: the journal rejects a purchase whose legs do not balance,
so recording an existing holding needs the cash it was bought with. Transfer in the cash **plus
the cost basis of the holdings**, then buy each holding at its original cost.

```bash
# 1. Opening cash (50,000) plus what the shares originally cost (15,000).
curl -X POST http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/transactions \
  -H 'content-type: application/json' \
  -d '{"request_id":"open-cash-1","transaction_type":"transfer_in","amount":"65000",
       "occurred_at":"2026-01-02T00:00:00Z","memo":"Opening balance"}'

# 2. Each existing holding, at the price actually paid.
curl -X POST http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/transactions \
  -H 'content-type: application/json' \
  -d '{"request_id":"open-aapl-1","transaction_type":"buy","ticker":"AAPL",
       "quantity":"100","unit_price":"150","occurred_at":"2026-01-02T00:00:00Z"}'
```

Use `transfer_in` rather than `deposit`: the money is not new capital contributed today, and
XIRR would otherwise report it as such. Use the original cost rather than today's price — valuing
an opening position at market erases the gain it already holds. Set `occurred_at` to the real
opening date, since that is where the NAV series and every return measurement start.

## Consolidating across currencies

Holdings in different currencies cannot simply be added. Group the portfolios and pick a
reporting currency:

```bash
curl -X POST http://127.0.0.1:8001/api/v1/portfolio-groups \
  -H 'content-type: application/json' \
  -d '{"name":"Everything","reporting_currency":"USD","portfolio_ids":["ID_A","ID_B"]}'

curl 'http://127.0.0.1:8001/api/v1/portfolio-groups/GROUP_ID/summary'
```

Every position keeps its local value beside the converted one, along with the rate used, the
path it took (direct, inverted, or crossed through an intermediary), and that rate's own date.
Read `converted_value_coverage_percent` and `unconverted` before using `total_value`: a pair
that cannot be resolved leaves the amount out of the total rather than converting it at a guess,
so the total may legitimately understate the group.

`issuer_exposure` aggregates listings of one company — an ADR and its local line — into a single
economic exposure while leaving the individual positions separate.

To stop reporting one portfolio in a group, replace the membership rather than deleting the
group: the dropped interval is closed, not erased, so earlier reports keep the members they had.
Deleting a group removes only the grouping — the portfolios, journals, and snapshots survive,
and the same group can be recreated.

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/portfolio-groups/GROUP_ID/members \
  -H 'content-type: application/json' -d '{"portfolio_ids":["ID_A"]}'

curl -X DELETE http://127.0.0.1:8001/api/v1/portfolio-groups/GROUP_ID
```

## Market research

History supports the original `?days=365` request and reproducible date ranges:

```bash
curl 'http://127.0.0.1:8001/api/v1/market/instruments/AAPL/history?start_date=2021-01-01&end_date=2026-07-24&interval=1d&adjustment=yfinance_auto_adjust'
```

`end_date` is inclusive even though yfinance's native `end` argument is exclusive. `days` cannot
be combined with `start_date` or `end_date`. Intervals are `1d`, `1wk`, and `1mo`; adjustment is
`yfinance_auto_adjust` or `unadjusted`. The response reports the requested range, actual first and
last observations, provider, fetch time, adjustment, and warnings.

For a fixed technical snapshot, pass the research report's data cutoff:

```bash
curl 'http://127.0.0.1:8001/api/v1/market/instruments/AAPL/technical-snapshot?as_of=2026-07-24&benchmark=%5EGSPC&event_date=2026-04-30&lookback_years=5'
```

The snapshot calculates SMA trend and slopes, period returns, RSI14, MACD, ATR14, realized
volatility, 252-bar drawdown, and volume statistics. An optional benchmark is aligned on common
observation dates. An optional event uses the first observation on or after `event_date`; its
anchored VWAP is an approximation based on daily typical price `(high + low + close) / 3`, weighted
by daily volume. Insufficient history produces `null` metrics and warnings rather than invented
values. Always inspect provider, actual as-of date, adjustment, and warnings, and combine technical
signals with fundamentals, valuation, and other evidence.

## Docker Compose

Build and start the service with its migrations applied automatically:

```bash
docker compose up --build -d
docker compose ps
```

This starts three services from the same image: the REST API at `http://127.0.0.1:8001`, the
MCP server (Streamable HTTP) at `http://127.0.0.1:8002/mcp`, and a snapshot-cron service with no
exposed port. Both the MCP and cron services call the API over the Docker network and start only
after the API is healthy. Exposed ports are bound to loopback only. SQLite data is persisted in
the `portfolio-data` named volume, so normal container rebuilds and restarts keep the portfolio.
Stop the services with `docker compose down`; add `--volumes` only when you intentionally want to
delete all portfolio data.

The cron service lists every portfolio and rebuilds its valuation snapshots once a day (default
00:00 UTC, `PORTFOLIO_SNAPSHOT_CRON_HOUR_UTC`) plus once at container startup, so daily NAV and
performance figures stay current without a manual `rebuild-snapshots` run. Each run covers a
rolling window (default the last 7 days, `PORTFOLIO_SNAPSHOT_LOOKBACK_DAYS`) rather than tracking
a last-run date: `rebuild_snapshots` skips dates that already have a snapshot, so the overlap is
cheap and a missed run or a late price correction is picked up automatically. Watch its output
with `docker compose logs portfolio-snapshot-cron`.

The dashboard uses relative paths for its assets and API calls, so it can be mounted behind a
reverse proxy under a subpath (e.g. `https://host/portfolio-manager/`). Set `PORTFOLIO_URL_PREFIX`
to that subpath (no trailing slash, e.g. `/portfolio-manager`) so the app can strip it from
incoming requests before routing and set the dashboard's `<base href>` accordingly -- this works
whether or not the browser's URL ends in a trailing slash, and needs no special header support
from the proxy, so it also covers proxies that cannot add custom headers (Tailscale Serve's
`--set-path`, for instance, forwards the full path unchanged):

```bash
tailscale serve --bg --set-path /portfolio-manager http://127.0.0.1:8001
```

```bash
PORTFOLIO_URL_PREFIX=/portfolio-manager uv run portfolio-manager
```

An nginx `location /portfolio-manager/ { proxy_pass http://127.0.0.1:8001/; }` block works the
same way, with or without `PORTFOLIO_URL_PREFIX` depending on whether it strips the prefix itself.
Without `PORTFOLIO_URL_PREFIX` the dashboard assumes it is mounted at the domain root.

## Agent integration

Use `http://127.0.0.1:8001/openapi.json` as the source for generated tools or an agent skill. The
schema includes stable `operationId` values, complete request examples, portfolio invariants,
retry guidance, error codes, quote freshness rules, and a recommended end-to-end workflow.

When wrapping the API as a skill, preserve these core instructions from the OpenAPI description:

- transactions record completed executions and never place orders;
- each portfolio accepts only instruments in its base currency;
- use a fresh `request_id` for each logical mutation and reuse it only for exact retries;
- check `stale`, quote timestamps, and `warnings` before making a market-dependent decision;
- send exact financial inputs as decimal strings and branch on machine-readable error `code`;
- report coverage alongside any derived figure — a partial snapshot, an unconverted currency, or
  an unruled cash event changes what a number means, and presenting it as settled is the failure
  this service is built to prevent.

## MCP server

An MCP server exposes every API operation as a tool for MCP clients such as Hermes and Claude
Desktop. It is a thin client over the HTTP API, so **the API must be running** (`uv run
portfolio-manager`) before you start it. Each tool matches an API `operationId` one-to-one
(`create_portfolio`, `record_transaction`, `get_portfolio_summary`, …), and errors preserve the same
`{code, message, details}` envelope.

Run over stdio (the default, for local clients that launch a subprocess):

```bash
uv run portfolio-mcp
```

Run over Streamable HTTP (for networked clients), served at `/mcp`:

```bash
PORTFOLIO_MCP_TRANSPORT=streamable-http uv run portfolio-mcp
```

Environment variables:

- `PORTFOLIO_API_BASE_URL` — API base URL (default `http://127.0.0.1:8001`).
- `PORTFOLIO_MCP_TRANSPORT` — `stdio` (default) or `streamable-http`.
- `PORTFOLIO_MCP_HOST` / `PORTFOLIO_MCP_PORT` — bind address for HTTP (default `127.0.0.1:8002`).

Register it in an MCP client. For a stdio client, point it at the command:

```json
{
  "mcpServers": {
    "portfolio-manager": {
      "command": "uv",
      "args": ["run", "portfolio-mcp"],
      "env": { "PORTFOLIO_API_BASE_URL": "http://127.0.0.1:8001" }
    }
  }
}
```

For an HTTP client, start the server in `streamable-http` mode and connect to
`http://127.0.0.1:8002/mcp`. The HTTP transport binds to loopback by default; exposing it beyond
loopback needs explicit approval, matching the API's security posture.

Market-research MCP clients can call:

```text
get_market_history(
  ticker="AAPL", start_date="2021-01-01", end_date="2026-07-24",
  interval="1d", adjustment="yfinance_auto_adjust"
)
get_technical_snapshot(
  ticker="AAPL", as_of="2026-07-24", benchmark="^GSPC",
  event_date="2026-04-30", lookback_years=5
)
```

Analysis clients can call:

```text
rebuild_valuation_snapshots(portfolio_id=..., start_date="2026-07-01", end_date="2026-07-31")
get_portfolio_performance(portfolio_id=..., start_date="2026-07-01", end_date="2026-07-31")
get_consolidated_summary(group_id=...)
get_instrument_profile(reference="TSM")
```

### Prompts and resources

Tools describe one operation each, which leaves two things unsaid: the order operations go in,
and the vocabulary a call must use. Four prompts and three resources carry that.

| Prompt | What it prevents |
| --- | --- |
| `open_account_with_holdings` | Opening cash must cover the holdings' cost, or the balanced journal rejects the purchase. Nothing in the tool list implies this. |
| `record_daily_activity` | Gross-vs-net dividends, capitalized fees, and positive-magnitude amounts. |
| `analyze_performance` | Snapshots must exist before a return can be measured, and `coverage` must be read with it. |
| `audit_data_quality` | Separates gaps a person can close from facts that no work will change. |

| Resource | Contents |
| --- | --- |
| `portfolio://conventions` | Tickers, decimals, idempotency, and every error code. |
| `portfolio://taxonomy` | Legal `asset_class` and `security_type` values with provenance ranking. |
| `portfolio://portfolios` | Live inventory of portfolios and groups, so a session skips discovery. |

The taxonomy and error-code lists are generated from the same enums the API validates against, and
tests fail if either drifts. Prompt support varies by client; Claude Desktop surfaces prompts as
slash commands, and clients without prompt support still get the same rules from the OpenAPI
description.

## Quality checks

```bash
uv run pytest
uv run ruff check .
```

Live Yahoo smoke tests are skipped by default. Run them explicitly when network access is
available:

```bash
uv run pytest -m external
```

Yahoo Finance data can be delayed, corrected after publication, vary by exchange, and has no
production SLA. yfinance is an unofficial adapter, and long-range intraday data is not offered by
these research endpoints. Auto-adjusted history rewrites OHLC for corporate actions; unadjusted
history can contain split/dividend discontinuities. Daily OHLCV cannot reproduce intraday paths,
so event anchored VWAP is explicitly an approximation. Quote responses identify provider and
local fetch timestamps and whether cached data is stale; research responses identify actual
observation dates, adjustment, fetch time, and data-quality warnings.
