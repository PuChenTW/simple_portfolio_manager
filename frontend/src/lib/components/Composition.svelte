<script lang="ts">
  import type { ConsolidatedSummary } from '../api/client'
  import { money, isNegative } from '../format'

  let { summary }: { summary: ConsolidatedSummary } = $props()

  const currency = $derived(summary.reporting_currency)

  // Composition is drawn from gross magnitudes, not from `total_value`. Net value is already
  // reduced by debt, so using it as the denominator would make the bands sum past 100%.
  const bands = $derived.by(() => {
    const securities = Math.abs(Number(summary.securities_value))
    const cash = Math.max(Number(summary.cash_value), 0)
    const debt = Math.abs(Number(summary.liabilities_value))
    const gross = securities + cash + debt
    if (!Number.isFinite(gross) || gross <= 0) return []
    return [
      { key: 'securities', label: 'Securities', value: securities, tone: 'accent' },
      { key: 'cash', label: 'Cash', value: cash, tone: 'positive' },
      { key: 'debt', label: 'Debt', value: debt, tone: 'negative' },
    ]
      .filter((b) => b.value > 0)
      .map((b) => ({ ...b, share: (b.value / gross) * 100 }))
  })
</script>

<section class="card">
  <h2>Composition</h2>

  {#if bands.length}
    <div class="bar" role="img" aria-label="Composition of gross value">
      {#each bands as band (band.key)}
        <span class="seg {band.tone}" style="width: {band.share}%"></span>
      {/each}
    </div>
    <ul class="legend">
      {#each bands as band (band.key)}
        <li>
          <span class="dot {band.tone}"></span>
          <span>{band.label}</span>
          <span class="num muted">{band.share.toFixed(1)}%</span>
        </li>
      {/each}
    </ul>
  {/if}

  <h3>Currency exposure</h3>
  <table>
    <thead>
      <tr>
        <th scope="col">Currency</th>
        <th scope="col" class="right">Local</th>
        <th scope="col" class="right">{currency}</th>
      </tr>
    </thead>
    <tbody>
      {#each summary.currency_exposure as row (row.currency)}
        <tr>
          <th scope="row">{row.currency}</th>
          <td class="right num" class:negative={isNegative(row.local_amount)}>
            {money(row.local_amount, row.currency)}
          </td>
          <td class="right num" class:negative={isNegative(row.reporting_amount)}>
            {row.reporting_amount == null ? '—' : money(row.reporting_amount, currency)}
          </td>
        </tr>
      {:else}
        <tr><td colspan="3" class="empty muted">No currency exposure recorded.</td></tr>
      {/each}
    </tbody>
  </table>
</section>

<style>
  .card {
    padding: var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  h2 {
    font-size: 15px;
    margin-bottom: 14px;
  }

  h3 {
    font-size: 13px;
    color: var(--text-muted);
    margin: 22px 0 8px;
  }

  .bar {
    display: flex;
    height: 12px;
    border-radius: 999px;
    overflow: hidden;
    background: var(--surface-sunken);
  }

  .seg.accent {
    background: var(--accent);
  }
  .seg.positive {
    background: var(--positive);
  }
  .seg.negative {
    background: var(--negative);
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
    list-style: none;
    margin: 12px 0 0;
    padding: 0;
    font-size: 13px;
  }

  .legend li {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
  }
  .dot.accent {
    background: var(--accent);
  }
  .dot.positive {
    background: var(--positive);
  }
  .dot.negative {
    background: var(--negative);
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  th,
  td {
    padding: 7px 8px;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }

  thead th {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-faint);
    font-weight: 600;
  }

  tbody tr:last-child th,
  tbody tr:last-child td {
    border-bottom: none;
  }

  .right {
    text-align: right;
  }

  .empty {
    text-align: center;
    padding: 16px;
  }
</style>
