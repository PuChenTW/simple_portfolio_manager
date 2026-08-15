# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the
codebase. This repo is **single-context**: one `CONTEXT.md` and one `docs/adr/` at the root.

## Before exploring, read these

- **`docs/ARCHITECTURE.md`** — already exists and carries the reasoning behind the journal, cache,
  valuation, transfer, and deployment subsystems. Read the relevant section before changing one.
  It is the closest thing this repo has to a written domain model today.
- **`CONTEXT.md`** at the repo root — the glossary, once it exists.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

If `CONTEXT.md` or `docs/adr/` don't exist, **proceed silently**. Don't flag their absence; don't
suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and
`/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

```
/
├── CONTEXT.md          ← created lazily by /domain-modeling
├── docs/
│   ├── ARCHITECTURE.md ← exists today
│   └── adr/            ← created lazily
└── src/portfolio_manager/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a
test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary
explicitly avoids.

Until `CONTEXT.md` exists, `AGENTS.md` and `docs/ARCHITECTURE.md` hold the operative vocabulary —
`event`, `leg`, `replay`, `projection`, `snapshot`, `portfolio kind`, `provenance` — and the
distinctions they draw are load-bearing. Reading `positions` for a historical question is a
vocabulary error before it is a code error.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing
language the project doesn't use (reconsider) or there's a real gap (note it for
`/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_

The five invariants in `AGENTS.md` are stronger than an ADR: they are not up for reopening in
passing. Contradicting one is a finding to raise with the user, not a trade-off to make inline.
