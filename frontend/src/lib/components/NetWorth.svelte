<script lang="ts">
  import type { ConsolidatedSummary } from '../api/client'
  import { money, percent, shortDate } from '../format'

  let { summary }: { summary: ConsolidatedSummary } = $props()

  const currency = $derived(summary.reporting_currency)
  const hasDebt = $derived(Number(summary.liabilities_value) !== 0)
  // Coverage below 100 means some holding never reached the reporting currency, so the hero
  // number is genuinely incomplete. That qualification belongs on the number, not in a side panel.
  const incomplete = $derived(Number(summary.converted_value_coverage_percent) < 100)
</script>

<section class="hero">
  <div class="headline">
    <p class="label">Net worth<span class="faint"> · {currency}</span></p>
    <p class="total num" class:negative={Number(summary.net_value) < 0}>
      {money(summary.net_value, currency)}
    </p>
    <p class="asof faint">as of {shortDate(summary.as_of)}</p>
  </div>

  {#if hasDebt}
    <!-- Liabilities stay negative so assets + liabilities == net and a reader cannot add
         where they should subtract. See docs/ARCHITECTURE.md, "Liability accounts". -->
    <div class="reconcile">
      <div class="line">
        <span class="muted">Assets</span>
        <span class="num">{money(summary.assets_value, currency)}</span>
      </div>
      <div class="line">
        <span class="muted">Liabilities</span>
        <span class="num negative">{money(summary.liabilities_value, currency)}</span>
      </div>
      <div class="line total-line">
        <span>Net</span>
        <span class="num">{money(summary.net_value, currency)}</span>
      </div>
    </div>
  {/if}
</section>

{#if incomplete}
  <div class="coverage">
    <strong>{percent(summary.converted_value_coverage_percent)}</strong> of value reached
    {currency}. Amounts below are excluded from the total rather than converted at a guessed rate.
    <ul>
      {#each summary.unconverted as item (item.currency + item.reason)}
        <li>
          <span class="num">{money(item.amount, item.currency)}</span>
          <span class="muted">— {item.reason}</span>
        </li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  .hero {
    display: flex;
    flex-wrap: wrap;
    gap: 32px;
    align-items: flex-start;
    justify-content: space-between;
    padding: 28px var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  .label {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--text-muted);
  }

  .total {
    margin: 0;
    font-size: clamp(30px, 6vw, 44px);
    font-weight: 650;
    letter-spacing: -0.02em;
  }

  .asof {
    margin: 6px 0 0;
    font-size: 12px;
  }

  .reconcile {
    min-width: 240px;
    flex: 0 1 auto;
  }

  .line {
    display: flex;
    justify-content: space-between;
    gap: 24px;
    padding: 5px 0;
    font-size: 13px;
  }

  .total-line {
    border-top: 1px solid var(--border);
    margin-top: 4px;
    padding-top: 8px;
    font-weight: 600;
  }

  .coverage {
    margin-top: 12px;
    padding: 12px 16px;
    background: var(--warning-soft);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-sm);
    font-size: 13px;
    color: var(--text);
  }

  .coverage ul {
    margin: 8px 0 0;
    padding-left: 18px;
  }

  @media (max-width: 640px) {
    .hero {
      flex-direction: column;
      gap: 20px;
    }
    .reconcile {
      width: 100%;
    }
  }
</style>
