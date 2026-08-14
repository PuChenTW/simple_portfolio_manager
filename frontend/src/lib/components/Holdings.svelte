<script lang="ts">
  import type { ConsolidatedSummary } from '../api/client'
  import { money, percent, quantity, safeWidth } from '../format'

  let { summary }: { summary: ConsolidatedSummary } = $props()

  const currency = $derived(summary.reporting_currency)

  // `weight_percent` divides by total value, which is net of debt -- a known rough edge in
  // consolidation.py. With a loan in the group the weights inflate (a single holding can read
  // 96%), so showing them would be actively misleading. Suppress rather than silently mislead.
  const weightsMeaningful = $derived(Number(summary.liabilities_value) === 0)

  const ranked = $derived(
    [...summary.positions].sort(
      (a, b) => Number(b.reporting_market_value ?? 0) - Number(a.reporting_market_value ?? 0),
    ),
  )
</script>

<section class="card">
  <header>
    <h2>Holdings</h2>
    <span class="faint">{ranked.length} position{ranked.length === 1 ? '' : 's'}</span>
  </header>

  {#if !weightsMeaningful && ranked.length}
    <p class="note muted">
      Weights are hidden because the group holds debt: they divide by net value, which double-counts
      leverage.
    </p>
  {/if}

  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th scope="col">Ticker</th>
          <th scope="col">Account</th>
          <th scope="col" class="right">Quantity</th>
          <th scope="col" class="right">Price</th>
          <th scope="col" class="right">Value ({currency})</th>
          {#if weightsMeaningful}<th scope="col" class="right weight-col">Weight</th>{/if}
        </tr>
      </thead>
      <tbody>
        {#each ranked as p (p.portfolio_id + p.ticker)}
          <tr>
            <th scope="row">
              {p.ticker}
              {#if p.warnings.length}
                <span class="flag" title={p.warnings.join('\n')}>!</span>
              {/if}
            </th>
            <td class="muted">{p.portfolio_name}</td>
            <td class="right num">{quantity(p.quantity)}</td>
            <td class="right num">
              {p.local_price == null ? '—' : money(p.local_price, p.local_currency)}
            </td>
            <td class="right num">
              {#if p.reporting_market_value == null}
                <span class="unpriced" title="Not converted; excluded from the total">—</span>
              {:else}
                {money(p.reporting_market_value, currency)}
              {/if}
            </td>
            {#if weightsMeaningful}
              <td class="right weight-col">
                <div class="weight">
                  <span class="num">{percent(p.weight_percent)}</span>
                  <span class="track"><span class="fill" style="width: {safeWidth(p.weight_percent)}%"></span></span>
                </div>
              </td>
            {/if}
          </tr>
        {:else}
          <tr>
            <td colspan={weightsMeaningful ? 6 : 5} class="empty muted">
              No securities in this group.
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
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
    margin-bottom: 12px;
  }

  h2 {
    font-size: 15px;
  }

  .note {
    margin: 0 0 12px;
    font-size: 12px;
  }

  /* Wide tables scroll inside their own container so the page body never scrolls sideways. */
  .scroll {
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    white-space: nowrap;
  }

  th,
  td {
    padding: 8px;
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

  .weight {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    justify-content: flex-end;
  }

  .track {
    width: 56px;
    height: 5px;
    border-radius: 999px;
    background: var(--surface-sunken);
    overflow: hidden;
  }

  .fill {
    display: block;
    height: 100%;
    background: var(--accent);
  }

  .flag {
    display: inline-block;
    width: 15px;
    height: 15px;
    line-height: 15px;
    text-align: center;
    border-radius: 50%;
    background: var(--warning-soft);
    color: var(--warning);
    font-size: 11px;
    font-weight: 700;
    cursor: help;
  }

  .unpriced {
    cursor: help;
    color: var(--text-faint);
  }

  .empty {
    text-align: center;
    padding: 20px;
  }

  @media (max-width: 720px) {
    .weight-col {
      display: none;
    }
  }
</style>
