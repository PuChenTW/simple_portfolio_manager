# Repository Guidelines

A local-first portfolio manager: a FastAPI service over SQLite that records an auditable
double-entry journal, values it historically, and exposes every endpoint to agents through MCP.
Python 3.13, `uv`, Alembic, Redis (optional), no frontend build step.

## Commands

- `uv sync` — create the environment from `uv.lock`.
- `uv run alembic upgrade head` — initialize or migrate the database.
- `uv run portfolio-manager` — run the API at `http://127.0.0.1:8001`.
- `uv run portfolio-mcp` — run the MCP server (stdio; needs the API running). Set
  `PORTFOLIO_MCP_TRANSPORT=streamable-http` for the HTTP transport.
- `uv run portfolio-admin rebuild-snapshots <portfolio_id> <start> <end>` — build daily
  valuation snapshots. Safe to re-run; existing dates are skipped.
- `uv run pytest` — deterministic tests; live Yahoo tests are skipped.
- `uv run pytest -m external` — exercise `AAPL`, `2330.TW`, `BTC-USD` online.
- `uv run ruff check .` — imports and style.
- `docker compose up --build -d` — build, migrate, and launch the container stack.

## Invariants

These five govern the data-transparency work and must not be weakened. They share one idea: a
value this service cannot determine is reported as undetermined, because an invented number is
indistinguishable from a real one once written.

1. **Never guess.** A classification, cost allocation, or trade-to-cash linkage that cannot be
   determined stays `unclassified` or `unresolved` with a warning.
2. **Never overwrite provenance.** A manual override outranks a provider value rather than
   replacing it, so retracting the override restores the original.
3. **Never post half an event.** Legs and their position/cash projections commit in one
   transaction, and posted events are corrected by reversal, never by edit or delete.
4. **Never convert at a guessed rate.** An unresolvable pair leaves the amount in its own
   currency, excluded from the converted total and listed in `unconverted` with a coverage
   percentage. Rates never come from after the report date, and every conversion reports its path.
5. **Never value a date with a later price.** Snapshots replay the journal to the cutoff and
   price from history bounded by that date. An unpriced holding is excluded from
   `securities_value`, carried at cost in `unpriced_market_value`, and makes the snapshot
   `partial`; a missing date in a series is reported, never interpolated.

A corollary for anything user-facing: a warning nobody can ever clear is worse than no warning,
because it teaches readers to ignore the ones that matter. Before reporting a gap, check that the
user can act on it and that it changes a number. A permanent fact about the data is not an open
task and belongs in a different count from gaps that genuinely bias a result.

## Architecture

Application code lives in `src/portfolio_manager/`, where most module names say what they hold.
The ones that don't, and the couplings that are easy to break:

- **`replay.py` is the only correct source of past state.** `positions` and `cash_balances` are
  present-state projections — reading them for a historical question silently returns today's
  answer with a past date on it.
- **`mcp_server.py` mirrors the whole API.** It is a thin HTTP client wrapping each endpoint as a
  tool, so a new endpoint without its tool ships a surface agents cannot reach. Its resources are
  generated from the enums they document so the two cannot drift.
- **`cache.py` is invisible by design** — a Redis layer wrapping the `MarketProvider` protocol
  that no consumer knows exists. Every failure degrades to the provider; `PORTFOLIO_REDIS_URL`
  unset disables it. Its month-bucketing rules are subtle and documented separately.
- **Persisted model changes need an Alembic migration** under `alembic/versions/`. The test
  harness builds tables from the ORM while production runs Alembic, so a missing migration passes
  tests and breaks deployment.
- **Keep `COPY src` below the dependency install in the `Dockerfile`.** Moving it above ties the
  whole dependency install to every source edit.

See @docs/ARCHITECTURE.md before changing the journal, cache, or valuation subsystems — each
encodes a decision that looks like overhead until you know what it prevents.

## Testing

Files are `test_*.py`, tests are `test_<behavior>`. Isolate API tests with the temporary SQLite
database and fake market provider from `tests/conftest.py`; normal tests must not require network
access. Add regression tests for accounting formulas, idempotency, error codes, migrations, and
OpenAPI changes.

`tests/legacy_api_baseline.json` freezes the 32 operations, 67 response models, and 32 MCP tools
published at version 0.4.0. Adding a new operation, model, or tool is fine. Touching one already
in the baseline is not, and the comparison is exact equality rather than containment: adding an
optional query parameter to an existing operation, or an optional field to an existing model,
fails `test_backward_compatibility.py` just as removing one does. That is deliberate — it forces
an addition to be a decision someone made rather than a diff nobody noticed. The fix is an
explicit version bump, never a quietly regenerated baseline. See @docs/ARCHITECTURE.md for the
version history and what each bump bought.

Three suites guard cross-cutting contracts: `test_migrations.py` asserts the migrated schema
matches the ORM models, since the harness builds tables from the ORM while production runs
Alembic. `test_mcp_server.py` asserts every API operation has a matching MCP tool, that
`portfolio://taxonomy` lists every enum member, that `portfolio://conventions` documents every
`DomainError` code raised anywhere in the package, and that no prompt names a nonexistent tool.

## Style

Ruff owns formatting and import rules; read `pyproject.toml` rather than restating them here.
Two conventions it cannot enforce:

- **Use `Decimal` for every financial value.** A binary float in accounting logic produces errors
  that survive every test with round numbers and surface only in production totals.
- Routes need stable `operation_id` values — they are the MCP tool names, so renaming one breaks
  the agent surface and the compatibility baseline.

## Security

Keep the service bound to loopback unless explicitly approved otherwise. Never commit database
files, credentials, or provider secrets. Trades record completed executions only: they do not
place orders or alter cash. Preserve single-currency portfolios, mutation `request_id`
idempotency, stale-quote warnings, and machine-readable errors.

## Verifying your work

- Run `uv run pytest` and `uv run ruff check .` before submitting.
- Dashboard changes must be verified with Playwright against the running service, not by
  inspection — `static/` is plain HTML, CSS, and vanilla JS with no build step and no external
  libraries, so nothing catches a broken selector but a browser. Say which widths you checked.
- A `docker compose` stack may already be running against real portfolio data. Check
  `docker compose ps` before assuming ports are free, and prefer a separate `PORTFOLIO_DB_PATH`
  and non-default port for manual testing.

Pull requests explain behavior changes, list verification commands, note schema migrations or
API compatibility impact, and include example requests/responses when the OpenAPI contract
changes.
