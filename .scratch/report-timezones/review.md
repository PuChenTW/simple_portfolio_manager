# Review receipt

Review base: `e80b523`.
Implementation: `fd81980`.
Corrections: `c743268`.

## Standards

Initial review found one P2: `_account_entries` gained a fourth argument.
The caller now resolves first-event dates. The helper accepts three arguments.
Follow-up reviewer found no remaining issues in the correction scope.
Standards verdict: PASS. Merge verdict: OK.

Initial reviewer run: `2fac7907-5ef8-4237-9b24-bfe2a9a9bf49`.
Follow-up reviewer run: `54cb179a-34a7-40c8-8cc4-7b9b6b311b51`.

## Spec

Initial review found two issues:

- P1: Pydantic accepted numeric occurred_at values without an explicit offset.
  The validator now checks the ISO input before coercion. Public API regressions cover four numeric cases.
- P2: Timezone validation rejected some IANA aliases and retained noncanonical spelling.
  The browser now saves canonical names from Intl. Tests cover aliases, lowercase names and offset rejection.

Follow-up reviewer confirmed both fixes. No new reachable defects or remaining spec conflicts qualified.
Merge verdict: OK.

Initial reviewer run: `86cca438-27f4-4215-a886-a6008a72a03d`.
Follow-up reviewer run: `5b7f50c8-6b7b-4472-8b38-1a334e5c8d71`.

## Verification at c743268

- `uv run pytest -q`: 452 passed, 3 external tests skipped.
- `uv run ruff check .`: passed.
- `bun run test`: 64 passed.
- `bun run check`: no errors or warnings.
- `bun run build`: passed.
- `bun run test:e2e`: 2 passed, at 1440px and 390px.
- Browser tests use a fresh migrated temporary database on loopback port 8017.
- Existing Docker services and real portfolio data remain unchanged.
