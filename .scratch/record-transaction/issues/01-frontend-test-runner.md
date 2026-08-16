# 01 — Install a frontend test runner

**What to build:** `bun run test` runs a test suite in `frontend/` and passes. No product
behaviour changes.

This is a prefactor. The frontend has no test runner and no test files today, so the cash-effect
arithmetic that ticket 03 introduces has nowhere to go. Landing the harness first means that
formula is written test-first rather than retrofitted — and it is the one piece of this feature
that can be wrong in a way inspection will not catch, because it re-implements `build_legs`'
arithmetic in TypeScript.

Vitest is the obvious choice: the project already builds with Vite, so the runner shares the
existing config rather than introducing a second toolchain.

Keep the scope to the harness. Do not add tests for existing components — that is a separate
decision about what is worth covering, and bundling it makes this ticket unbounded.

**Blocked by:** None — can start immediately.

**Status:** resolved

- [x] `bun run test` is a script in `frontend/package.json` and exits zero
- [x] At least one trivial test file proves the runner resolves TypeScript and the project's
      path conventions
- [x] `bun run check` and `bun run build` still pass
- [x] The runner is a dev dependency; nothing ships in the built bundle

## Comments

Implemented in `594efb8`. Every box above was verified against a
scratch `PORTFOLIO_DB_PATH` on port 8011 with Playwright at widths 375, 768, and 1280;
the running Compose stack was left untouched.
