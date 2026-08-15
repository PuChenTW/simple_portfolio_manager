<script lang="ts">
  import type { PortfolioSummary } from '../../api/client'
  import { money, percent, quantity, safeWidth, shortDate } from '../../format'

  let { summary }: { summary: PortfolioSummary } = $props()

  // A cash or liability account is refused any security by `postings.py`, so its empty positions
  // table is not an empty state someone could fill -- it is a permanent fact about the account.
  // Rendering it would put a row of headers over "no positions" on every visit, forever.
  const holdsSecurities = $derived(summary.portfolio.kind === 'investment')

  // A single portfolio is single-currency by construction, so every figure here is already in
  // the same unit -- no conversion, no coverage caveat, and no unconverted list.
  const currency = $derived(summary.portfolio.base_currency)

  const ranked = $derived(
    [...summary.positions].sort((a, b) => Number(b.market_value) - Number(a.market_value)),
  )

  const stale = $derived(summary.positions.filter((p) => p.price_stale))

  function sign(value: string): 'positive' | 'negative' | '' {
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed === 0) return ''
    return parsed > 0 ? 'positive' : 'negative'
  }
</script>

<div class="stack">
  <section class="tiles">
    <div class="tile">
      <!-- "Total value" is wrong for a debt: the figure is what is owed, and calling it value
           invites a reader to add it where they should subtract. -->
      <p class="label">
        {summary.portfolio.kind === 'liability' ? 'Outstanding' : 'Total value'}
      </p>
      <p class="figure num" class:negative={Number(summary.total_value) < 0}>
        {money(summary.total_value, currency)}
      </p>
      <p class="faint sub">as of {shortDate(summary.valuation_as_of)}</p>
    </div>
    <!-- A positionless book has no securities and no P&L by construction, so those tiles would
         read NT$0 forever. Its total *is* its cash, which the first tile already states. -->
    {#if holdsSecurities}
      <div class="tile">
        <p class="label">Securities</p>
        <p class="figure num">{money(summary.securities_value, currency)}</p>
        <p class="faint sub">{ranked.length} position{ranked.length === 1 ? '' : 's'}</p>
      </div>
      <div class="tile">
        <p class="label">Cash</p>
        <p class="figure num" class:negative={Number(summary.cash_value) < 0}>
          {money(summary.cash_value, currency)}
        </p>
        <p class="faint sub">{summary.cash.currency}</p>
      </div>
      <div class="tile">
        <p class="label">Total P&amp;L</p>
        <p class="figure num {sign(summary.total_pnl)}">{money(summary.total_pnl, currency)}</p>
        <!-- Split because the two are earned differently: realized is banked, unrealized is a
             mark that can still reverse. One combined figure hides which it is. -->
        <p class="faint sub">
          {money(summary.realized_pnl, currency)} realized ·
          {money(summary.unrealized_pnl, currency)} unrealized
        </p>
      </div>
    {/if}
  </section>

  {#if summary.warnings?.length}
    <ul class="warnings">
      {#each summary.warnings as warning (warning)}
        <li>{warning}</li>
      {/each}
    </ul>
  {/if}

  {#if holdsSecurities}
  <section class="card">
    <header>
      <h2>Positions</h2>
      {#if stale.length}
        <!-- `price_stale` means a refresh failed and a cached quote was used -- an actionable
             fact, unlike a closed market, which is never flagged. -->
        <span class="faint">{stale.length} priced from a stale quote</span>
      {/if}
    </header>

    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">Ticker</th>
            <th scope="col" class="right">Quantity</th>
            <th scope="col" class="right">Avg cost</th>
            <th scope="col" class="right">Price</th>
            <th scope="col" class="right">Value</th>
            <th scope="col" class="right">Unrealized</th>
            <th scope="col" class="right weight-col">Weight</th>
          </tr>
        </thead>
        <tbody>
          {#each ranked as p (p.ticker)}
            <tr>
              <th scope="row">
                <span class="ticker">{p.ticker}</span>
                {#if p.price_stale}
                  <span class="flag" title="Valued from an expired cached quote">!</span>
                {/if}
                <span class="name faint">{p.name}</span>
                {#if p.tags.length}
                  <span class="tags">
                    {#each p.tags as tag (tag)}<span class="tag">{tag}</span>{/each}
                  </span>
                {/if}
              </th>
              <td class="right num">{quantity(p.quantity)}</td>
              <td class="right num">{money(p.average_cost, p.currency)}</td>
              <td class="right num">{money(p.current_price, p.currency)}</td>
              <td class="right num">{money(p.market_value, currency)}</td>
              <td class="right num {sign(p.unrealized_pnl)}">
                {money(p.unrealized_pnl, currency)}
                {#if p.unrealized_pnl_percent != null}
                  <span class="pct">{percent(p.unrealized_pnl_percent)}</span>
                {/if}
              </td>
              <td class="right weight-col">
                <div class="weight">
                  <span class="num">{percent(p.weight_percent)}</span>
                  <span class="track">
                    <span class="fill" style="width: {safeWidth(p.weight_percent)}%"></span>
                  </span>
                </div>
              </td>
            </tr>
          {:else}
            <tr>
              <td colspan="7" class="empty muted">No open positions in this account.</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
  {/if}
</div>

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* Wrapping flex rather than a fixed four-column grid: a cash or liability book renders one
     tile, and a grid track would stretch it the full width of the page. */
  .tiles {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }

  .tile {
    flex: 1 1 220px;
    max-width: 340px;
  }

  .tile {
    padding: 16px var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  .label {
    margin: 0 0 4px;
    font-size: 12px;
    color: var(--text-muted);
  }

  .figure {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  .sub {
    margin: 4px 0 0;
    font-size: 11px;
  }

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
    gap: 12px;
    margin-bottom: 12px;
  }

  h2 {
    font-size: 15px;
  }

  .warnings {
    margin: 0;
    padding: 12px 16px 12px 34px;
    background: var(--warning-soft);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-sm);
    font-size: 13px;
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

  .ticker {
    font-weight: 600;
  }

  .name {
    display: block;
    font-weight: 400;
    font-size: 11px;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tags {
    display: inline-flex;
    gap: 4px;
    margin-top: 3px;
  }

  .tag {
    padding: 0 6px;
    font-size: 10px;
    font-weight: 400;
    color: var(--text-faint);
    background: var(--surface-sunken);
    border-radius: 999px;
  }

  .pct {
    display: block;
    font-size: 11px;
    opacity: 0.8;
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

  .empty {
    text-align: center;
    padding: 20px;
  }

  /* The flex basis handles tile wrapping on its own; only the table needs a breakpoint. */
  @media (max-width: 720px) {
    .weight-col {
      display: none;
    }
  }

  @media (max-width: 460px) {
    .tile {
      max-width: none;
    }
  }
</style>
