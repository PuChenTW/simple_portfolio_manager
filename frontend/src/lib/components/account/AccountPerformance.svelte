<script lang="ts">
  import type { NavHistory, Performance } from '../../api/client'
  import { RANGE_PRESETS, type RangePresetId } from '../../account.svelte'
  import { money, percent, shortDate } from '../../format'
  import { router } from '../../route.svelte'
  import NavChart from './NavChart.svelte'

  let {
    performance,
    navHistory,
    range,
    activePreset,
    onrange,
    onpreset,
  }: {
    performance: Performance
    navHistory: NavHistory | null
    range: { start: string; end: string }
    /** Which preset produced `range`, or null for a hand-picked one. Owned by the page rather
     *  than this component: loading a range replaces this component with a placeholder, so state
     *  held here would not survive the click that set it. */
    activePreset: RangePresetId | null
    onrange: (start: string, end: string) => void
    onpreset: (id: RangePresetId) => void
  } = $props()

  const currency = $derived(performance.base_currency)
  const coverage = $derived(performance.coverage)

  // A gap is discovered here and fixed on the settings tab, so the warning links to it rather
  // than describing where to go.
  const settingsHref = $derived(router.account(performance.portfolio_id, 'settings'))

  // Draft dates, so typing one end does not fire a request before the other is chosen. Re-seeded
  // whenever the applied range changes underneath the form.
  let start = $state('')
  let end = $state('')
  $effect(() => {
    start = range.start
    end = range.end
  })

  const invalidRange = $derived(start > end)

  function apply(event: SubmitEvent): void {
    event.preventDefault()
    if (!invalidRange) onrange(start, end)
  }

  function sign(value: string | null): 'positive' | 'negative' | '' {
    if (value == null) return ''
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed === 0) return ''
    return parsed > 0 ? 'positive' : 'negative'
  }

  /** Only dates that actually have a snapshot are plotted. Missing dates are never bridged with
   *  a straight line -- an interpolated segment is indistinguishable from a real one, which is
   *  what invariant 5 forbids. They are counted and reported below the chart instead. */
  const plotted = $derived(
    (navHistory?.snapshots ?? []).filter((s) => Number.isFinite(Number(s.total_value))).length,
  )
</script>

<div class="stack">
  <form class="range card" onsubmit={apply}>
    <label>
      <span class="label">From</span>
      <input type="date" bind:value={start} max={end} />
    </label>
    <label>
      <span class="label">To</span>
      <input type="date" bind:value={end} min={start} />
    </label>
    <button type="submit" disabled={invalidRange}>Update</button>

    <!-- Presets apply on click rather than filling the inputs and waiting for Update. The draft
         dates exist so typing one end does not fire a request before the other is chosen; one
         click carries no such ambiguity. -->
    <div class="presets" role="group" aria-label="Preset ranges">
      {#each RANGE_PRESETS as preset (preset.id)}
        <button
          type="button"
          class="preset"
          class:active={activePreset === preset.id}
          aria-pressed={activePreset === preset.id}
          onclick={() => onpreset(preset.id)}
        >
          {preset.label}
        </button>
      {/each}
    </div>

    {#if invalidRange}
      <p class="invalid negative">The start date must not be after the end date.</p>
    {/if}
  </form>

  <!-- Coverage sits above the returns, not below them. A number nobody can trust must not be
       read before the reason it cannot be trusted. -->
  {#if !coverage.is_reliable}
    <div class="coverage">
      <strong>These returns are not reliable.</strong>
      <ul>
        {#each coverage.warnings as warning (warning)}
          <li>{warning}</li>
        {/each}
      </ul>
      <!-- The server's warnings already name each cause; this line is the one thing they do not
           say -- what to do about the most common one. Restating the counts above it would
           print the same fact twice. -->
      {#if coverage.missing_dates.length}
        <p class="fix">
          Build the missing dates under <a href={settingsHref}>Settings</a>. They are reported
          rather than interpolated, so the gap closes only once they exist.
        </p>
      {/if}
    </div>
  {/if}

  <section class="returns">
    <div class="tile">
      <p class="label">
        Time-weighted return
        <span class="help" title={performance.twr_method_description}>?</span>
      </p>
      <p class="figure num {sign(performance.twr_percent)}">
        {percent(performance.twr_percent, 2)}
      </p>
      <p class="faint sub">
        {#if performance.annualized_twr_percent != null}
          {percent(performance.annualized_twr_percent, 2)} annualized ·
        {/if}
        {performance.twr_method}
      </p>
    </div>

    <div class="tile">
      <p class="label">
        Money-weighted return
        <span class="help" title={performance.xirr_method_description}>?</span>
      </p>
      {#if performance.xirr_percent == null}
        <!-- Never an opaque blank: the API always says why, and the reason is the answer. -->
        <p class="figure muted">—</p>
        <p class="faint sub">{performance.xirr_unavailable_reason ?? 'Not available.'}</p>
      {:else}
        <p class="figure num {sign(performance.xirr_percent)}">
          {percent(performance.xirr_percent, 2)}
        </p>
        <p class="faint sub">{performance.xirr_method}</p>
      {/if}
    </div>
  </section>

  <section class="card">
    <header>
      <h2>Value over the period</h2>
      <span class="faint">
        {shortDate(performance.start_date)} – {shortDate(performance.end_date)}
      </span>
    </header>

    {#if plotted >= 2}
      <NavChart
        {navHistory}
        {currency}
        beginningValue={performance.beginning_value}
      />
      <p class="faint sub">
        {plotted} snapshots plotted.
        {#if navHistory?.missing_dates.length}
          {navHistory.missing_dates.length} date{navHistory.missing_dates.length === 1 ? '' : 's'}
          in range have none and are omitted rather than joined by a straight line.
        {/if}
      </p>
    {:else}
      <p class="empty muted">
        Fewer than two snapshots in this range, so there is no series to plot. Build them under
        <a href={settingsHref}>Settings</a>.
      </p>
    {/if}

    <dl class="ends">
      <div><dt>Beginning value</dt><dd class="num">{money(performance.beginning_value, currency)}</dd></div>
      <div><dt>Ending value</dt><dd class="num">{money(performance.ending_value, currency)}</dd></div>
    </dl>
  </section>

  <section class="card">
    <header><h2>Cash flows</h2></header>
    <!-- External flows are investor capital crossing the boundary; income, fees, and taxes are
         portfolio activity. TWR removes the first and keeps the second, which is why they are
         listed separately rather than netted into one number. -->
    <dl class="flows">
      <div>
        <dt>External inflows</dt>
        <dd class="num positive">{money(performance.external_inflows, currency)}</dd>
      </div>
      <div>
        <dt>External outflows</dt>
        <dd class="num negative">{money(performance.external_outflows, currency)}</dd>
      </div>
      <div><dt>Income</dt><dd class="num">{money(performance.income, currency)}</dd></div>
      <div><dt>Fees</dt><dd class="num">{money(performance.fees, currency)}</dd></div>
      <div><dt>Taxes</dt><dd class="num">{money(performance.taxes, currency)}</dd></div>
    </dl>
  </section>
</div>

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: 16px;
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

  .range {
    display: flex;
    align-items: flex-end;
    gap: 12px;
    flex-wrap: wrap;
  }

  .range label {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .label {
    font-size: 12px;
    color: var(--text-muted);
  }

  input[type='date'] {
    padding: 6px 10px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
  }

  .range button {
    padding: 7px 14px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface-sunken);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .range button:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* Pushed to the far edge so the presets read as a separate control rather than a fourth form
     field. `.range` already wraps, so this drops to its own line when the row runs out of width. */
  .presets {
    display: flex;
    margin-left: auto;
  }

  .presets .preset {
    padding: 7px 12px;
    border-radius: 0;
    margin-left: -1px;
  }

  .presets .preset:first-child {
    border-radius: var(--radius-sm) 0 0 var(--radius-sm);
    margin-left: 0;
  }

  .presets .preset:last-child {
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  }

  .presets .preset.active {
    z-index: 1;
    color: var(--accent);
    background: var(--accent-soft);
    border-color: var(--accent);
  }

  .invalid {
    flex-basis: 100%;
    margin: 0;
    font-size: 12px;
  }

  .returns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }

  .tile {
    padding: 16px var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  .tile .label {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0 0 4px;
  }

  .figure {
    margin: 0;
    font-size: 26px;
    font-weight: 650;
    letter-spacing: -0.02em;
  }

  .sub {
    margin: 6px 0 0;
    font-size: 11px;
  }

  .help {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--surface-sunken);
    color: var(--text-faint);
    font-size: 10px;
    cursor: help;
  }

  .coverage {
    padding: 12px 16px;
    background: var(--warning-soft);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-sm);
    font-size: 13px;
  }

  .coverage ul {
    margin: 8px 0 0;
    padding-left: 18px;
  }

  .coverage li + li {
    margin-top: 4px;
  }

  .fix {
    margin: 8px 0 0;
    font-size: 12px;
  }

  .ends,
  .flows {
    display: grid;
    gap: 8px 24px;
    margin: 16px 0 0;
  }

  .ends {
    grid-template-columns: repeat(2, 1fr);
  }

  .flows {
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    margin: 0;
  }

  .ends div,
  .flows div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  dt {
    font-size: 12px;
    color: var(--text-muted);
  }

  dd {
    margin: 0;
    font-size: 15px;
    font-weight: 500;
  }

  .empty {
    padding: 20px 0;
    text-align: center;
  }

  @media (max-width: 640px) {
    .returns {
      grid-template-columns: 1fr;
    }
    .ends {
      grid-template-columns: 1fr;
    }
  }
</style>
