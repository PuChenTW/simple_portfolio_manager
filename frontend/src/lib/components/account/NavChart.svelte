<script lang="ts">
  import { Area, Axis, Chart, Highlight, Layer, Tooltip } from 'layerchart'
  import { scaleTime } from 'd3-scale'
  import type { NavHistory } from '../../api/client'
  import { compactMoney, money, percent, shortDate } from '../../format'

  let {
    navHistory,
    currency,
    beginningValue,
  }: {
    navHistory: NavHistory | null
    currency: string
    /** The range's opening value, so the tooltip can say how far a date sits from the start. */
    beginningValue: string | null
  } = $props()

  /** The NAV series, positioned by real date rather than by index.
   *
   * Index spacing would draw a three-month gap exactly as wide as a weekend, which is merely
   * misleading until the axis carries dates -- then it puts a labelled date above a point from a
   * different date. Gaps stay unbridged either way: invariant 5 forbids inventing the segment. */
  const points = $derived(
    (navHistory?.snapshots ?? [])
      .map((s) => ({ date: new Date(s.valuation_date), value: Number(s.total_value) }))
      .filter((p) => Number.isFinite(p.value) && !Number.isNaN(p.date.valueOf())),
  )

  const opening = $derived(Number(beginningValue))

  /** Change from the range's opening value. "Where was I on this date" is usually asked as
   *  "how far up was I by then". Null when there is no opening value to measure against. */
  function changeFrom(value: number): { absolute: string; percent: string } | null {
    if (!Number.isFinite(opening) || opening === 0) return null
    const delta = value - opening
    return {
      absolute: money(String(delta), currency),
      percent: percent(String((delta / Math.abs(opening)) * 100), 2),
    }
  }
</script>

{#if points.length >= 2}
  <div class="chart">
    <Chart
      data={points}
      x="date"
      xScale={scaleTime()}
      y="value"
      yNice
      yDomain={[null, null]}
      padding={{ left: 56, bottom: 24, top: 8, right: 8 }}
      tooltipContext={{ mode: 'bisect-x' }}
    >
      <Layer type="svg">
        <!-- Ticks establish scale, so they are abbreviated. The exact figure belongs in the
             tooltip, where it was asked for. -->
        <Axis
          placement="left"
          grid
          rule
          ticks={4}
          format={(v: number) => compactMoney(String(v), currency)}
        />
        <Axis placement="bottom" rule tickSpacing={90} />
        <Area line={{ class: 'nav-line' }} class="nav-area" />
        <!-- `bisect-x` snaps to the nearest real snapshot. An interpolated reading between two
             plotted points would be indistinguishable from a measured one. -->
        <Highlight points lines />
      </Layer>

      <Tooltip.Root>
        {#snippet children({ data }: { data: { date: Date; value: number } })}
          {@const change = changeFrom(data.value)}
          <Tooltip.Header>{shortDate(data.date.toISOString())}</Tooltip.Header>
          <Tooltip.List>
            <Tooltip.Item label="Value" value={money(String(data.value), currency)} />
            {#if change}
              <Tooltip.Item label="Change" value="{change.absolute} ({change.percent})" />
            {/if}
          </Tooltip.List>
        {/snippet}
      </Tooltip.Root>
    </Chart>
  </div>
{/if}

<style>
  .chart {
    height: 200px;
  }

  /* Every color comes from the dashboard's own tokens, so the light/dark toggle keeps working
     and the chart does not read as pasted in. */
  .chart :global(.nav-area) {
    fill: var(--accent-soft);
  }

  .chart :global(.nav-line) {
    stroke: var(--accent);
    stroke-width: 1.5;
  }

  .chart :global(.lc-axis text) {
    fill: var(--text-muted);
    font-family: var(--font-num);
    font-size: 11px;
  }

  /* Gridlines are scaffolding, not data. LayerChart's default is a near-black line that competes
     with the series; at token contrast the eye reads them as background. */
  .chart :global(.lc-axis-grid) {
    stroke: var(--border);
  }

  .chart :global(.lc-axis-rule),
  .chart :global(.lc-axis-tick) {
    stroke: var(--border-strong);
  }

  /* The tooltip is portaled to the end of <body>, so it escapes this component's scoping and a
     `.chart`-prefixed rule never reaches it. Left unstyled it ships LayerChart's light default,
     which renders near-white text on a white panel in dark mode. */
  :global(.lc-tooltip-content) {
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    box-shadow: 0 4px 12px rgb(0 0 0 / 0.15);
    color: var(--text);
    font-family: var(--font-num);
    font-size: 12px;
  }

  :global(.lc-tooltip-header) {
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
  }

  :global(.lc-tooltip-item-label) {
    color: var(--text-muted);
  }

  :global(.lc-tooltip-item-value) {
    color: var(--text);
  }
</style>
