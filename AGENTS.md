# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/portfolio_manager/`. Keep HTTP routing and OpenAPI metadata in
`api.py`, request/response contracts in `schemas.py`, database tables in `models.py`, portfolio
rules in `services.py`, and Yahoo market-data logic in `market.py`. The MCP server in
`mcp_server.py` is a thin HTTP client that wraps each API endpoint as a tool; add a matching tool
there whenever you add or change an endpoint. SQLite setup and runtime
configuration belong in `db.py` and `config.py`. Database migrations live under
`alembic/versions/`; add one whenever persisted models change. Tests live in `tests/`, with fakes
in `tests/conftest.py`. Docker deployment files are
`Dockerfile` and `compose.yaml`.

Data-transparency modules build on that core. `taxonomy.py` holds the asset-class and
security-type vocabulary plus provenance ranking; `identity.py` resolves stable instrument IDs,
issuer mapping, and classification precedence. `journal.py` defines event and leg vocabulary with
the balance validator, `postings.py` performs atomic posting and reversal, and
`corporate_actions.py` records, previews, and applies issuer events. `backfill.py` migrates legacy
rows into the journal and is exposed through `cli.py`.

Historical valuation builds on the journal. `replay.py` rebuilds positions, cash, and flow
totals at any cutoff by folding journal legs, and is the only correct source of past state --
`positions` and `cash_balances` describe the present. `valuation.py` prices a replayed state
with history bounded by the valuation date and stores it as a snapshot, plus the re-runnable
range rebuild behind `portfolio-admin rebuild-snapshots`. `flows.py` holds manual rulings on
whether a migrated cash movement was investor capital or portfolio activity; the pre-journal
model could not distinguish them, and replay reads an active ruling in place of the value
derived from the event type. `performance.py` computes TWR and XIRR from stored snapshots and
journal flows, and reports the coverage behind them: only unruled *cash* events bias a return,
so migrated trades (which carry no cash leg) are counted separately and do not raise an alarm.
`fx.py` resolves point-in-time exchange rates -- direct, inverted, or crossed -- and stores every
observation so a conversion can be audited; `consolidation.py` groups portfolios and expresses
their holdings in one reporting currency, keeping each local figure beside its converted one.

## Build, Test, and Development Commands

- `uv sync`: create the Python 3.13 environment from `uv.lock`.
- `uv run alembic upgrade head`: initialize or migrate the SQLite database.
- `uv run portfolio-manager`: run the API at `http://127.0.0.1:8001`.
- `uv run portfolio-mcp`: run the MCP server (stdio; needs the API running). Set
  `PORTFOLIO_MCP_TRANSPORT=streamable-http` for the HTTP transport.
- `uv run portfolio-admin backfill-journal`: migrate legacy trades and cash into journal events.
- `uv run portfolio-admin verify-journal <portfolio_id>`: compare stored cash against the journal.
- `uv run portfolio-admin rebuild-snapshots <portfolio_id> <start> <end>`: build daily valuation
  snapshots over a date range. Safe to re-run; existing dates are skipped.
- `uv run portfolio-admin review-flows <portfolio_id>`: list migrated cash events awaiting a
  ruling on whether they crossed the portfolio boundary, with the evidence for each suggestion.
- `uv run portfolio-admin set-flow <event_id> <external|internal> --reason "..."`: record that
  ruling. `--retract` withdraws it and restores the derived value.
- `uv run pytest`: run deterministic tests; live Yahoo tests are skipped.
- `uv run pytest -m external`: exercise `AAPL`, `2330.TW`, and `BTC-USD` online.
- `uv run ruff check .`: enforce imports and Python style.
- `docker compose up --build -d`: build, migrate, and launch the containerized service.

## Coding Style & Naming Conventions

Use four-space indentation, Python type hints, and a 100-character line limit. Ruff enforces
`E`, `F`, `I`, `UP`, `B`, and `SIM` rules. Prefer small single-purpose functions, early returns,
and concrete data structures. Use `snake_case` for modules/functions/fields, `PascalCase` for
classes and Pydantic models, and stable `operation_id` values for routes. Preserve
`Decimal` arithmetic for financial values; never introduce binary floats into accounting logic.

## Testing Guidelines

Pytest files use `test_*.py` and tests use `test_<behavior>`. Isolate API tests with the temporary
SQLite database and fake market provider from `conftest.py`; normal tests must not require network
access. Add regression tests for accounting formulas, idempotency, error codes, migrations, and
OpenAPI changes. Run both pytest and Ruff before submitting changes.

`tests/legacy_api_baseline.json` freezes the 15 operations, 33 response models, and 15 MCP tools
that existed before the identity and journal work. Adding to the surface is fine; changing or
removing anything in the baseline fails `test_backward_compatibility.py` and requires an explicit
version bump rather than a regenerated baseline. `test_migrations.py` additionally asserts that
the migrated schema matches the ORM models, since the test harness builds tables from the ORM
while production runs Alembic.

## Commit & Pull Request Guidelines

The repository has no commit history yet. Use concise imperative commits such as
`Add stale quote fallback` or `Document trade idempotency`. Keep unrelated changes separate. Pull
requests should explain behavior changes, list verification commands, note schema migrations
or API compatibility impact, and include example requests/responses when the OpenAPI contract
changes. Screenshots are unnecessary for this API-only project.

## Security & Agent-Specific Rules

Keep the service bound to loopback unless explicitly approved otherwise. Never commit database
files, credentials, or provider secrets. Trades record completed executions only: they do not
place orders or alter cash. Preserve single-currency portfolios, mutation `request_id`
idempotency, stale-quote warnings, and machine-readable `{code, message, details}` errors.

Three invariants govern the data-transparency work and must not be weakened:

1. **Never guess.** A classification, cost allocation, or trade-to-cash linkage that cannot be
   determined stays `unclassified`, `unresolved`, or `unlinked_legacy` with a warning. An invented
   value is indistinguishable from a real one once written and silently corrupts everything
   derived from it.
2. **Never overwrite provenance.** A manual override outranks a provider value rather than
   replacing it, so retracting the override restores the original.
3. **Never post half an event.** Legs and their position/cash projections commit in one
   transaction, and posted events are corrected by reversal, never by edit or delete.
4. **Never convert at a guessed rate.** A currency pair that cannot be resolved leaves the
   amount in its own currency, excluded from the converted total and listed in `unconverted`
   with the coverage percentage. A total that quietly omitted it would look complete and be
   wrong. Rates never come from after the report date, and every conversion reports its path.
5. **Never value a date with a later price.** Snapshots replay the journal to the cutoff and
   price it from history bounded by that date. A holding with no price on or before the date is
   excluded from `securities_value`, carried at cost in `unpriced_market_value`, and makes the
   snapshot `partial`; a missing date in a series is reported, never interpolated.
