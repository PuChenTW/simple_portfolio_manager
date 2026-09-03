<script lang="ts">
  import { api, type ConsolidatedSummary, type Portfolio, type PortfolioKind } from '../api/client'
  import { money } from '../format'
  import { router } from '../route.svelte'

  let {
    portfolios,
    summary,
  }: { portfolios: Portfolio[]; summary: ConsolidatedSummary | null } = $props()

  const memberIds = $derived(new Set(summary?.portfolio_ids ?? []))
  let accountValues = $state<Record<string, string | null>>({})

  // The group summary cannot be split back into accounts: cash is aggregated by currency and
  // conversion failures are deliberately left unresolved. Ask the existing account endpoint for
  // each server-computed total instead of reconstructing a number in the browser.
  $effect(() => {
    const accounts = portfolios
    let active = true
    accountValues = {}

    for (const portfolio of accounts) {
      void api.portfolioSummary(portfolio.id).then(
        (value) => {
          if (active) accountValues[portfolio.id] = value.total_value
        },
        () => {
          if (active) accountValues[portfolio.id] = null
        },
      )
    }

    return () => {
      active = false
    }
  })

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
          <span class="identity">
            <a class="name" href={router.account(p.id)}>{p.name}</a>
            <span class="meta faint">
              {p.base_currency}{p.institution ? ` · ${p.institution}` : ''}
            </span>
          </span>
          <span class="measure">
            {#if !memberIds.has(p.id)}
              <span class="tag" title="Not part of the selected group">outside group</span>
            {/if}
            <span
              class="amount num"
              class:negative={Number(accountValues[p.id]) < 0}
              title={p.kind === 'investment' ? 'Account value' : 'Account balance'}
            >
              {#if accountValues[p.id] === undefined}
                <span class="faint" aria-label="Loading account value">…</span>
              {:else}
                {money(accountValues[p.id], p.base_currency)}
              {/if}
            </span>
          </span>
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
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: baseline;
    gap: 12px;
    padding: 7px 0;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }

  li:last-child {
    border-bottom: none;
  }

  .name {
    font-weight: 500;
    color: var(--text);
    text-decoration: none;
  }

  .name:hover {
    color: var(--accent);
    text-decoration: underline;
  }

  .name:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 2px;
  }

  .meta {
    margin-left: 4px;
    font-size: 12px;
  }

  .identity {
    min-width: 0;
  }

  .measure {
    display: flex;
    align-items: baseline;
    justify-content: flex-end;
    gap: 8px;
  }

  .amount {
    min-width: 7ch;
    text-align: right;
    white-space: nowrap;
  }

  .outside .name {
    color: var(--text-muted);
    font-weight: 400;
  }

  .outside .name:hover {
    color: var(--accent);
  }

  .tag {
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
