# Local Portfolio Manager

A local FastAPI service designed for agents that need to track cash, US and Taiwan stocks,
and cryptocurrencies. It keeps an auditable transaction ledger, calculates moving-average
cost and profit/loss, and caches Yahoo Finance quotes with explicit timestamps.

## Setup

```bash
uv sync
uv run alembic upgrade head
uv run portfolio-manager
```

The server listens on `http://127.0.0.1:8001`. OpenAPI documentation is available at
`http://127.0.0.1:8001/docs`. The read-only Traditional Chinese portfolio dashboard is available
at `http://127.0.0.1:8001/`; it reads the existing portfolios and their latest summaries without
changing trades or cash. The service remains bound to loopback only.

Set `PORTFOLIO_DB_PATH` to choose the SQLite database file. Quote data is fresh for 300
seconds by default; override it with `PORTFOLIO_QUOTE_TTL_SECONDS`.

## Typical flow

Create a USD portfolio:

```bash
curl -X POST http://127.0.0.1:8001/api/v1/portfolios \
  -H 'content-type: application/json' \
  -d '{"name":"US long term","base_currency":"USD"}'
```

Use the returned portfolio ID to deposit cash and record a trade. `request_id` should be a
new UUID or another client-generated unique value for every logical mutation; retrying the
same payload with the same ID is safe.

```bash
curl -X POST http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/cash-transactions \
  -H 'content-type: application/json' \
  -d '{"request_id":"cash-001","action":"deposit","amount":"10000"}'

curl -X POST http://127.0.0.1:8001/api/v1/portfolios/PORTFOLIO_ID/trades \
  -H 'content-type: application/json' \
  -d '{"request_id":"trade-001","ticker":"AAPL","side":"buy","quantity":"10","unit_price":"200","fee":"1"}'
```

Read the full valuation at `/api/v1/portfolios/PORTFOLIO_ID/summary`. Prices, quantities,
and amounts are JSON decimal strings. All timestamps are UTC RFC 3339 values.

Ticker conventions are `AAPL` for US stocks, `2330.TW` for TWSE, `8069.TWO` for TPEX, and
`BTC-USD` for crypto. A portfolio only accepts assets whose quote currency matches its base
currency; use separate portfolios for TWD and USD holdings. Trades and cash are deliberately
managed separately and trades never alter cash automatically.

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

This starts two services from the same image: the REST API at `http://127.0.0.1:8001` and the
MCP server (Streamable HTTP) at `http://127.0.0.1:8002/mcp`. The MCP service calls the API over
the Docker network and starts only after the API is healthy. Both ports are bound to loopback
only. SQLite data is persisted in the `portfolio-data` named volume, so normal container rebuilds
and restarts keep the portfolio. Stop the services with `docker compose down`; add `--volumes`
only when you intentionally want to delete all portfolio data.

## Agent integration

Use `http://127.0.0.1:8001/openapi.json` as the source for generated tools or an agent skill. The
schema includes stable `operationId` values, complete request examples, portfolio invariants,
retry guidance, error codes, quote freshness rules, and a recommended end-to-end workflow.

When wrapping the API as a skill, preserve these core instructions from the OpenAPI description:

- trades record completed executions and never place orders or alter cash;
- each portfolio accepts only instruments in its base currency;
- use a fresh `request_id` for each logical mutation and reuse it only for exact retries;
- check `stale`, quote timestamps, and `warnings` before making a market-dependent decision;
- send exact financial inputs as decimal strings and branch on machine-readable error `code`.

## MCP server

An MCP server exposes every API operation as a tool for MCP clients such as Hermes and Claude
Desktop. It is a thin client over the HTTP API, so **the API must be running** (`uv run
portfolio-manager`) before you start it. Each tool matches an API `operationId` one-to-one
(`create_portfolio`, `record_trade`, `get_portfolio_summary`, …), and errors preserve the same
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
