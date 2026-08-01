# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/portfolio_manager/`. Keep HTTP routing and OpenAPI metadata in
`api.py`, request/response contracts in `schemas.py`, database tables in `models.py`, portfolio
rules in `services.py`, and Yahoo market-data logic in `market.py`. The MCP server in
`mcp_server.py` is a thin HTTP client that wraps each API endpoint as a tool; add a matching tool
there whenever you add or change an endpoint. That module also holds the MCP prompts and
resources: prompts carry multi-step workflows and the mistakes their ordering prevents, resources
carry the vocabulary and conventions a call needs before any tool is chosen. Generate a resource
from the enum it documents rather than restating it, so the two cannot drift. SQLite setup and runtime
configuration belong in `db.py` and `config.py`. Database migrations live under
`alembic/versions/`; add one whenever persisted models change. Tests live in `tests/`, with fakes
in `tests/conftest.py`. The read-only dashboard in `static/` is plain HTML, CSS, and vanilla JS
with no build step and no external libraries -- the NAV chart is hand-rolled SVG. Verify frontend
changes with Playwright against the running service, not by inspection. Docker deployment files are
`Dockerfile` and `compose.yaml`. The Dockerfile installs dependencies from `pyproject.toml` and
`uv.lock` alone, before `src/` is copied, so a source edit rebuilds in about a second instead of
resyncing every dependency. Keep `COPY src` below that step; moving it above ties the whole
dependency install to every source change.

Data-transparency modules build on that core. `taxonomy.py` holds the asset-class and
security-type vocabulary plus provenance ranking; `identity.py` resolves stable instrument IDs,
issuer mapping, and classification precedence. `journal.py` defines event and leg vocabulary with
the balance validator, `postings.py` performs atomic posting and reversal, and
`corporate_actions.py` records, previews, and applies issuer events.

Historical valuation builds on the journal. `replay.py` rebuilds positions, cash, and flow
totals at any cutoff by folding journal legs, and is the only correct source of past state --
`positions` and `cash_balances` describe the present. `valuation.py` prices a replayed state
with history bounded by the valuation date and stores it as a snapshot, plus the re-runnable
range rebuild behind `portfolio-admin rebuild-snapshots`. `performance.py` computes TWR and XIRR
from stored snapshots and journal flows, and reports the coverage behind them: a gap, a partial
valuation, or an event whose cash flow cannot be classified each makes a return unreliable.
`fx.py` resolves point-in-time exchange rates -- direct, inverted, or crossed -- and stores every
observation so a conversion can be audited; `consolidation.py` groups portfolios and expresses
their holdings in one reporting currency, keeping each local figure beside its converted one.

## Build, Test, and Development Commands

- `uv sync`: create the Python 3.13 environment from `uv.lock`.
- `uv run alembic upgrade head`: initialize or migrate the SQLite database.
- `uv run portfolio-manager`: run the API at `http://127.0.0.1:8001`.
- `uv run portfolio-mcp`: run the MCP server (stdio; needs the API running). Set
  `PORTFOLIO_MCP_TRANSPORT=streamable-http` for the HTTP transport.
- `uv run portfolio-admin rebuild-snapshots <portfolio_id> <start> <end>`: build daily valuation
  snapshots over a date range. Safe to re-run; existing dates are skipped.
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

`tests/legacy_api_baseline.json` freezes the 32 operations, 67 response models, and 32 MCP tools
published at version 0.2.0. Adding to the surface is fine; changing or removing anything in the
baseline fails `test_backward_compatibility.py` and requires an explicit version bump rather than
a regenerated baseline. Version 0.2.0 was such a bump: it removed the pre-journal `record_trade`
and `record_cash_transaction` ledgers, whose position and cash writes were independent and could
disagree. `record_transaction` is now the only write path. `test_migrations.py` additionally asserts that
the migrated schema matches the ORM models, since the test harness builds tables from the ORM
while production runs Alembic. `test_mcp_server.py` asserts every API operation has a matching
MCP tool, so adding an endpoint without its tool fails the suite rather than shipping a surface
agents cannot reach. It also asserts that `portfolio://taxonomy` lists every enum member, that
`portfolio://conventions` documents every `DomainError` code raised anywhere in the package, and
that no prompt names a tool that does not exist -- a workflow referencing a removed tool fails
mid-task, which is worse than having no workflow at all.

A warning nobody can ever clear is worse than no warning: it teaches readers to ignore the ones
that matter. Before reporting a gap, check whether the user can actually act on it, and whether it
actually changes a number. A permanent fact about the data is not an open task, and it belongs in
a different count from the gaps that genuinely bias a result.

## Commit & Pull Request Guidelines

Use concise imperative commits such as `Add stale quote fallback` or `Document trade
idempotency`. Keep unrelated changes separate. Pull requests should explain behavior changes,
list verification commands, note schema migrations or API compatibility impact, and include
example requests/responses when the OpenAPI contract changes. Dashboard changes should say what
was verified in a browser and at which widths.

## Security & Agent-Specific Rules

Keep the service bound to loopback unless explicitly approved otherwise. Never commit database
files, credentials, or provider secrets. Trades record completed executions only: they do not
place orders or alter cash. Preserve single-currency portfolios, mutation `request_id`
idempotency, stale-quote warnings, and machine-readable `{code, message, details}` errors.

Five invariants govern the data-transparency work and must not be weakened. They share one idea:
a value this service cannot determine is reported as undetermined, because an invented number is
indistinguishable from a real one once written.

1. **Never guess.** A classification, cost allocation, or trade-to-cash linkage that cannot be
   determined stays `unclassified` or `unresolved` with a warning. An invented
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
