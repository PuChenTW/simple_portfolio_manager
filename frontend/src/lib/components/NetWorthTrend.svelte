<script lang="ts">
  import { Area, Axis, Chart, Highlight, Layer, Rule, Tooltip } from 'layerchart'
  import { scaleTime } from 'd3-scale'
  import type { NavSeries } from '../api/client'
  import { compactMoney, money, percent, shortDate } from '../format'

  let {
    series,
    loading,
    error,
  }: { series: NavSeries | null; loading: boolean; error: string | null } = $props()

  type Range = '1M' | '3M' | '1Y' | 'YTD' | 'All'
  const RANGES: Range[] = ['1M', '3M', '1Y', 'YTD', 'All']
  let range = $state<Range>('1Y')

  const currency = $derived(series?.reporting_currency ?? '')

  /** Every point, positioned by real date.
   *
   * Index spacing would draw a three-month gap exactly as wide as a weekend, which puts a
   * labelled date above a point from a different date once the axis carries dates. */
  const all = $derived(
    (series?.points ?? [])
      .map((point) => ({
        date: new Date(point.valuation_date),
        value: Number(point.net_value),
        assets: point.assets_value,
        liabilities: point.liabilities_value,
        accounts: point.contributing_account_count,
        coverage: point.converted_value_coverage_percent,
        partial: point.status !== 'complete',
      }))
      .filter((p) => Number.isFinite(p.value) && !Number.isNaN(p.date.valueOf())),
  )

  const last = $derived(all.length ? all[all.length - 1].date : null)

  function cutoff(from: Date, choice: Range): Date | null {
    const start = new Date(from)
    switch (choice) {
      case '1M':
        start.setMonth(start.getMonth() - 1)
        return start
      case '3M':
        start.setMonth(start.getMonth() - 3)
        return start
      case '1Y':
        start.setFullYear(start.getFullYear() - 1)
        return start
      case 'YTD':
        return new Date(from.getFullYear(), 0, 1)
      case 'All':
        return null
    }
  }

  // The whole series is fetched once, so a range change filters what is already here rather than
  // asking the server again. Switching is a view change, not a round trip.
  const points = $derived.by(() => {
    if (!last) return []
    const from = cutoff(last, range)
    return from ? all.filter((p) => p.date >= from) : all
  })

  const opening = $derived(points.length ? points[0].value : null)
  const closing = $derived(points.length ? points[points.length - 1].value : null)

  /** Change across the visible window, and whether it is a comparison at all.
   *
   * Subtracting the first point from the last is only a return when both sum the same accounts.
   * Over this data's default 1Y window that is false -- two accounts at the start, eleven at the
   * end -- and the difference reads as +42% when most of it is accounts arriving. A headline
   * figure is the most quoted thing on a chart, so it either states that it is not comparable or
   * it is a lie in the largest type on the panel. */
  const change = $derived.by(() => {
    if (opening === null || closing === null || opening === 0) return null
    if (!points.length) return null
    const from = points[0].accounts
    const to = points[points.length - 1].accounts
    const delta = closing - opening
    return {
      absolute: money(String(delta), currency),
      percent: percent(String((delta / Math.abs(opening)) * 100), 2),
      negative: delta < 0,
      comparable: from === to,
      from,
      to,
    }
  })

  /** The change since the last time the account count changed.
   *
   * Without this the panel can only ever say "not comparable", which is true and useless -- a
   * dead end teaches readers to ignore the notice. There is almost always a comparable window;
   * it just does not line up with a round 1M or 1Y button, so it is named rather than snapped
   * to. Snapping would make the button labels lie about the period they select. */
  const stable = $derived.by(() => {
    if (all.length < 2) return null
    const count = all[all.length - 1].accounts
    let index = all.length - 1
    while (index > 0 && all[index - 1].accounts === count) index -= 1
    if (index === 0) return null // The whole series is comparable; the headline already says so.
    const from = all[index]
    const to = all[all.length - 1]
    if (from.value === 0) return null
    const delta = to.value - from.value
    const days = Math.round((to.date.valueOf() - from.date.valueOf()) / 86_400_000)
    return {
      since: from.date,
      days,
      absolute: money(String(delta), currency),
      percent: percent(String((delta / Math.abs(from.value)) * 100), 2),
      negative: delta < 0,
    }
  })

  /** Account entries inside the visible window, one marker per date.
   *
   * Grouped by date because eight accounts opened on one day is one event -- one change in what
   * the line measures -- not eight. Eight overlapping rules would be an unreadable fence at the
   * exact place the chart most needs explaining. */
  const markers = $derived.by(() => {
    if (!points.length) return []
    const first = points[0].date
    const final = points[points.length - 1].date
    const byDate = new Map<string, string[]>()
    for (const entry of series?.account_entries ?? []) {
      const day = new Date(entry.entered_on)
      if (day < first || day > final) continue
      const key = entry.entered_on
      const names = byDate.get(key)
      if (names) names.push(entry.portfolio_name)
      else byDate.set(key, [entry.portfolio_name])
    }
    return [...byDate.entries()].map(([day, names]) => ({ date: new Date(day), names }))
  })

  // The composition warning is the one that changes how the line is read, so it sits with the
  // chart rather than in the page's general warning list.
  const composition = $derived(
    (series?.warnings ?? []).find((w) => w.includes('changing set of accounts')) ?? null,
  )
  const gaps = $derived(
    (series?.warnings ?? []).filter((w) => !w.includes('changing set of accounts')),
  )
</script>

<section class="panel">
  <header>
    <div class="titles">
      <h2>Net worth over time</h2>
      {#if change && points.length > 1}
        {#if change.comparable}
          <p class="change faint">
            <span class:negative={change.negative}>{change.absolute} ({change.percent})</span>
            over {range === 'All' ? 'all history' : range}
          </p>
        {:else}
          <!-- The figure is withheld rather than qualified. A struck-through or asterisked
               percentage still gets read and quoted; the sentence that replaces it cannot be. -->
          <p class="change incomparable">
            No comparable change over {range === 'All' ? 'all history' : range}: this window
            starts with {change.from} account{change.from === 1 ? '' : 's'} and ends with
            {change.to}.
            {#if stable}
              Since all {change.to} have been recorded ({stable.days} days), net worth is
              <span class="figure" class:negative={stable.negative}>
                {stable.absolute} ({stable.percent})</span
              >.
            {/if}
          </p>
        {/if}
      {/if}
    </div>

    {#if all.length > 1}
      <div class="ranges" role="group" aria-label="Time range">
        {#each RANGES as choice (choice)}
          <button
            type="button"
            class:current={range === choice}
            aria-pressed={range === choice}
            onclick={() => (range = choice)}
          >
            {choice}
          </button>
        {/each}
      </div>
    {/if}
  </header>

  {#if loading}
    <div class="state faint">Building the series…</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else if points.length < 2}
    <div class="state faint">
      Not enough snapshots in this range to draw a line. Build them with a snapshot rebuild.
    </div>
  {:else}
    <div class="chart">
      <Chart
        data={points}
        x="date"
        xScale={scaleTime()}
        y="value"
        yNice
        yDomain={[null, null]}
        padding={{ left: 64, bottom: 24, top: 8, right: 8 }}
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
          <Area line={{ class: 'trend-line' }} class="trend-area" />

          <!-- Where the line changes what it measures. Without these the chart reports an
               account arriving as a gain. -->
          {#each markers as marker (marker.date.valueOf())}
            <Rule x={marker.date} class="entry-rule" />
          {/each}

          <!-- `bisect-x` snaps to the nearest real point. An interpolated reading between two
               plotted points would be indistinguishable from a measured one. -->
          <Highlight points lines />
        </Layer>

        <Tooltip.Root>
          {#snippet children({ data }: { data: (typeof points)[number] })}
            <Tooltip.Header>{shortDate(data.date.toISOString())}</Tooltip.Header>
            <Tooltip.List>
              <Tooltip.Item label="Net worth" value={money(String(data.value), currency)} />
              <Tooltip.Item label="Assets" value={money(data.assets, currency)} />
              {#if Number(data.liabilities) !== 0}
                <Tooltip.Item label="Liabilities" value={money(data.liabilities, currency)} />
              {/if}
              <!-- Always "N of M", never "N": a bare count does not warn anyone, while the
                   contrast with today says at a glance whether two dates are comparable. -->
              <Tooltip.Item
                label="Accounts"
                value="{data.accounts} of {series?.total_account_count ?? data.accounts}"
              />
              {#if Number(data.coverage) < 100}
                <Tooltip.Item label="Converted" value={percent(data.coverage)} />
              {/if}
            </Tooltip.List>
          {/snippet}
        </Tooltip.Root>
      </Chart>
    </div>

    {#if markers.length}
      <ul class="entries">
        {#each markers as marker (marker.date.valueOf())}
          <li>
            <span class="tick" aria-hidden="true"></span>
            <span class="when">{shortDate(marker.date.toISOString())}</span>
            <span class="who">{marker.names.join(', ')} enter{marker.names.length > 1 ? '' : 's'}</span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if composition}
      <p class="note">{composition}</p>
    {/if}

    {#if gaps.length}
      <ul class="warnings">
        {#each gaps as warning (warning)}
          <li>{warning}</li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  .panel {
    padding: 20px var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }

  h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }

  .change {
    margin: 4px 0 0;
    font-size: 12px;
  }

  .change span {
    font-variant-numeric: tabular-nums;
    color: var(--positive, var(--text));
  }

  .change span.negative {
    color: var(--negative);
  }

  .incomparable {
    margin: 4px 0 0;
    max-width: 58ch;
    font-size: 12px;
    color: var(--text-muted);
  }

  /* Emphasised because it is the figure that *is* comparable, and the sentence around it is
     what stops the withheld one being reached for instead. */
  .incomparable .figure {
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }

  .incomparable .figure.negative {
    color: var(--negative);
  }

  .ranges {
    display: flex;
    gap: 2px;
    padding: 2px;
    background: var(--surface-sunken);
    border-radius: var(--radius-sm);
  }

  .ranges button {
    padding: 4px 10px;
    font: inherit;
    font-size: 12px;
    color: var(--text-faint);
    background: transparent;
    border: 1px solid transparent;
    border-radius: calc(var(--radius-sm) - 2px);
    cursor: pointer;
  }

  .ranges button.current {
    color: var(--text);
    background: var(--surface);
    border-color: var(--border-strong);
  }

  .chart {
    height: 260px;
  }

  .chart :global(.trend-area) {
    fill: color-mix(in srgb, var(--accent) 14%, transparent);
  }

  .chart :global(.trend-line) {
    stroke: var(--accent);
    stroke-width: 1.5;
  }

  /* Thin and dashed: it must be findable when read against the line, and never mistaken for
     data. */
  .chart :global(.entry-rule) {
    stroke: var(--text-faint);
    stroke-width: 1;
    stroke-dasharray: 3 3;
    opacity: 0.55;
  }

  .entries {
    margin: 10px 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 18px;
    font-size: 12px;
    color: var(--text-muted);
  }

  .entries li {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }

  .tick {
    width: 0;
    height: 9px;
    border-left: 1px dashed var(--text-faint);
    align-self: center;
  }

  .when {
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }

  .note {
    margin: 10px 0 0;
    font-size: 12px;
    color: var(--text-muted);
  }

  .warnings {
    margin: 10px 0 0;
    padding: 10px 14px 10px 30px;
    background: var(--warning-soft);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-sm);
    font-size: 12px;
  }

  .state {
    padding: 48px 0;
    text-align: center;
    font-size: 13px;
  }

  .error {
    color: var(--negative);
  }

  @media (max-width: 640px) {
    .chart {
      height: 200px;
    }
    header {
      flex-direction: column;
      gap: 10px;
    }
  }
</style>
