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

## Docker Compose

Build and start the service with its migrations applied automatically:

```bash
docker compose up --build -d
docker compose ps
```

The API remains local-only at `http://127.0.0.1:8001`. SQLite data is persisted in the
`portfolio-data` named volume, so normal container rebuilds and restarts keep the portfolio.
Stop the service with `docker compose down`; add `--volumes` only when you intentionally want
to delete all portfolio data.

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

Yahoo Finance data can be delayed and has no production SLA. Every response identifies the
provider timestamp, local fetch timestamp, and whether a cached value is stale.
