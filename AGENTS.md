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

## Build, Test, and Development Commands

- `uv sync`: create the Python 3.13 environment from `uv.lock`.
- `uv run alembic upgrade head`: initialize or migrate the SQLite database.
- `uv run portfolio-manager`: run the API at `http://127.0.0.1:8001`.
- `uv run portfolio-mcp`: run the MCP server (stdio; needs the API running). Set
  `PORTFOLIO_MCP_TRANSPORT=streamable-http` for the HTTP transport.
- `uv run portfolio-admin backfill-journal`: migrate legacy trades and cash into journal events.
- `uv run portfolio-admin verify-journal <portfolio_id>`: compare stored cash against the journal.
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
