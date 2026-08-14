<script lang="ts">
  import type { ConsolidatedSummary, Portfolio, PortfolioKind } from '../api/client'

  let {
    portfolios,
    summary,
  }: { portfolios: Portfolio[]; summary: ConsolidatedSummary | null } = $props()

  const memberIds = $derived(new Set(summary?.portfolio_ids ?? []))

  // An unknown kind is a book this client cannot interpret; it must not default to `investment`.
  // See docs/ARCHITECTURE.md, API version history 0.6.0.
  const SECTIONS: { kind: PortfolioKind; label: string }[] = [
    { kind: 'investment', label: 'Investment' },
    { kind: 'cash', label: 'Cash' },
    { kind: 'liability', label: 'Liabilities' },
  ]

  const known = $derived(new Set(SECTIONS.map((s) => s.kind as string)))
  const sections = $derived([
    ...SECTIONS.map((s) => ({
      ...s,
      items: portfolios.filter((p) => p.kind === s.kind),
    })),
    { kind: 'other' as const, label: 'Other', items: portfolios.filter((p) => !known.has(p.kind)) },
  ].filter((s) => s.items.length))
</script>

<section class="card">
  <header>
    <h2>Accounts</h2>
    <span class="faint">{portfolios.length} total</span>
  </header>

  {#each sections as section (section.kind)}
    <h3>{section.label}</h3>
    <ul>
      {#each section.items as p (p.id)}
        <li class:outside={!memberIds.has(p.id)}>
          <span class="name">{p.name}</span>
          <span class="meta faint">
            {p.base_currency}{p.institution ? ` · ${p.institution}` : ''}
          </span>
          {#if !memberIds.has(p.id)}
            <span class="tag" title="Not part of the selected group">outside group</span>
          {/if}
        </li>
      {/each}
    </ul>
  {/each}

  {#if !portfolios.length}
    <p class="empty muted">No portfolios yet.</p>
  {/if}
</section>

<style>
  .card {
    padding: var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 8px;
  }

  h2 {
    font-size: 15px;
  }

  h3 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-faint);
    margin: 16px 0 6px;
  }

  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  li {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    padding: 7px 0;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }

  li:last-child {
    border-bottom: none;
  }

  .name {
    font-weight: 500;
  }

  .meta {
    font-size: 12px;
  }

  .outside .name {
    color: var(--text-muted);
    font-weight: 400;
  }

  .tag {
    margin-left: auto;
    padding: 1px 7px;
    border: 1px dashed var(--border-strong);
    border-radius: 999px;
    font-size: 11px;
    color: var(--text-faint);
  }

  .empty {
    padding: 12px 0;
  }
</style>
