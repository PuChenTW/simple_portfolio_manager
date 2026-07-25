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

## Build, Test, and Development Commands

- `uv sync`: create the Python 3.13 environment from `uv.lock`.
- `uv run alembic upgrade head`: initialize or migrate the SQLite database.
- `uv run portfolio-manager`: run the API at `http://127.0.0.1:8001`.
- `uv run portfolio-mcp`: run the MCP server (stdio; needs the API running). Set
  `PORTFOLIO_MCP_TRANSPORT=streamable-http` for the HTTP transport.
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
