# Dashboard

The dashboard: Svelte 5 and TypeScript, served at `/`. It replaced a vanilla-JS page that lived
in `src/portfolio_manager/static/`, so that whole directory is now this project's build output.

## Commands

- `bun install` — install dependencies.
- `bun run dev` — dev server with hot reload, proxying `/api` to `PORTFOLIO_API_URL`
  (default `http://127.0.0.1:8003`).
- `bun run build` — build into `src/portfolio_manager/static/`, where FastAPI serves it.
- `bun run check` — typecheck Svelte and TypeScript.
- `bun run types` — regenerate `src/lib/api/schema.d.ts` from a running API.

The build output is gitignored: it is an artifact, produced at image build time.

## Types come from the API, not from hand-written interfaces

`src/lib/api/schema.d.ts` is generated from the live `/openapi.json` by `openapi-typescript`.
The backend freezes its response models in `tests/legacy_api_baseline.json`, so a contract change
is a deliberate version bump — regenerating turns that bump into a compile error here rather than
an `undefined` at runtime. Regenerate whenever the API version changes; never hand-edit it.

## Two rules that are easy to break

**Every URL stays relative.** Assets build with `base: './'` and API calls resolve against
`../api/v1/` from the page's own directory. Tailscale Serve strips its path prefix and sends no
`X-Forwarded-Prefix`, so a proxied request is byte-identical to a direct one at the app, and no
server-side prefix config can tell them apart.
One absolute path silently breaks whichever deployment it was not written for. `test_api.py`
asserts every emitted asset path starts with `./`.

**Money is formatted, never computed.** The API sends decimal strings to avoid binary float
error. `format.ts` parses them for display only. Any total shown must come from a field the
server computed — a sum done here would reintroduce exactly the error `Decimal` exists to prevent.
The one exception is `Composition.svelte`, which derives display-only percentages of a bar.

## What the front page will and will not show

The landing view is the group summary: net worth, then composition, then holdings, then accounts —
decreasing altitude, so a reader forms a whole-account picture before drilling in.

It deliberately shows **no group-level return**. `get_portfolio_performance` and `get_nav_history`
are per-portfolio only, and transfers classify as `external` flows, so summing member snapshots
would double-count money moved between accounts. A group TWR belongs with the group-level flow
work described in `docs/ARCHITECTURE.md`, not before it.

Holdings **hide weights when the group holds debt**. `weight_percent` divides by total value,
which is net of liabilities, so a single position can read 96% in a leveraged group. That is the
known rough edge recorded in `docs/ARCHITECTURE.md`; showing the number would be misleading, so
the column is suppressed with a note rather than silently rendered.

Coverage below 100% is shown **on the hero number**, not in a separate panel, because an
unconvertible amount is excluded from that exact total. A warning that changes a number belongs
next to the number it changes.
