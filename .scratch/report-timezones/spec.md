# Explicit transaction times and report timezones

Review base: `e80b523`.

## Decisions

- API version 0.11.0 requires an offset or Z on supplied transaction `occurred_at` values.
- Omitted transaction times still default to server time. Reject future times with server time in the error.
- Normalize supplied times to UTC. Do not rewrite existing journal events.
- The browser defaults to its timezone. A local setting selects browser-following or a named IANA timezone.
- Manual entries require date and minute-precision time. Initialize when opened, do not tick, and provide Set to now.
- Preserve date, time, and timezone after a successful posting.
- Reject nonexistent local times. Require offset selection for ambiguous local times.
- Display transactions and interpret report dates in the effective timezone.
- API and MCP report timezone defaults to UTC.
- Current summaries replay through now. Historical days stop before the next local midnight.
- Preserve market-date semantics and never use prices after the cutoff.
- Store snapshot timezone explicitly. Never relabel UTC snapshots or mix timezone series.
- Recompute grouping when the report timezone changes. Missing snapshots remain missing until explicitly rebuilt.
- Keep existing UTC journal data unchanged. Corrections use reversal and reposting.

## Agreed test seams

1. Public API and MCP: validation, UTC normalization, report cutoffs, snapshot isolation, compatibility.
2. Frontend transaction preparation and timezone settings: defaults, persistence, DST and timestamps.
3. Browser against an isolated database: posting, dashboard refresh, timezone changes, repeated entry, Set to now.

## Verification

Run focused tests during implementation. Run frontend check and build before browser verification.
Check desktop and mobile widths. Run full pytest and Ruff once at completion.
Review Standards and Spec independently against the review base. Commit on the current branch.

## Implementation notes

- Migration `0010` preserves old snapshot amounts and position rows, and labels their timezone UTC.
- Calculation revision `v2` bounds securities by completed regular sessions.
- FX, crypto and unknown-market daily closes use completed UTC days. This is conservative because daily bars do not provide an intraday observation time.
- Old `v1` snapshots remain stored. Rebuild explicitly for the required revision and timezone.
- Browser tests also exposed a missing dashboard invalidation after posting or reversal. Both paths now reload the dashboard.
- `docs/ARCHITECTURE.md`, referenced by repository instructions, is absent. The existing ADR and CONTEXT.md supply the available architecture guidance.

## Verification before review

- `uv run pytest -q`: 448 passed, 3 external tests skipped.
- `uv run ruff check .`: passed.
- `bun run test`: 63 passed.
- `bun run check`: no errors or warnings.
- `bun run build`: passed.
- `bun run test:e2e`: passed at 1440px and 390px against a fresh migrated temporary SQLite database on port 8017.
- Existing Docker services on ports 8001 and 8002 were not modified.

## Review findings and corrections

### Standards

- P2: `_account_entries` gained a fourth argument. Resolve event dates in the caller and keep three arguments.

### Spec

- P1: Numeric transaction timestamps bypassed offset validation through Pydantic coercion. Validate ISO inputs before coercion. API regressions cover numeric values and numeric strings.
- P2: Browser timezone validation accepted noncanonical spelling and rejected valid aliases without a slash. Save and send the canonical IANA name from Intl. Preserve alias support and reject fixed-offset names.

All three findings are accepted and corrected. Follow-up review checks their resolution.
Final revalidation: 452 Python tests passed, 3 external tests skipped. Frontend tests: 64 passed.
Ruff, typecheck, build and both Playwright widths passed again.
