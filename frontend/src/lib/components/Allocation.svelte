<script lang="ts">
  import type { AssetClass, ConsolidatedSummary } from '../api/client'
  import { compactMoney, money, percent } from '../format'

  let { summary }: { summary: ConsolidatedSummary } = $props()

  const currency = $derived(summary.reporting_currency)

  type Mode = 'holding' | 'class'
  let mode = $state<Mode>('class')

  // Colour follows the entity, never its rank: a slot is pinned to an asset class so switching
  // groups or classifying a holding cannot repaint the survivors. Cash is a class here even
  // though the taxonomy reaches it through a portfolio's currency rather than an instrument.
  //
  // Every class that can appear gets its own slot, because two exposures sharing one colour is
  // the one thing a categorical palette must never do. The validated palette carries exactly
  // eight identity hues, and the taxonomy has nine members -- so rather than invent a ninth
  // colour, `cash_equivalent` folds into `cash` at the bucketing step below. That fold is
  // economic, not cosmetic: a cash equivalent is cash for the purpose of reading an allocation,
  // which is why it is the one pair that can merge without losing a distinction the chart is for.
  // Keyed by AssetClass rather than string, so a new taxonomy member is a build error here
  // instead of a holding silently drawn in the "no identity" gray.
  //
  // Every member is listed: a class either owns a slot, or names the class it folds into. A new
  // taxonomy member fails to compile until someone decides which it is, rather than being drawn
  // silently in the "no identity" gray.
  const CLASS_SLOT: Record<AssetClass, number | { foldInto: AssetClass }> = {
    equity: 1,
    fixed_income: 2,
    cash: 3,
    commodity: 4,
    crypto: 5,
    real_estate: 6,
    multi_asset: 7,
    alternative: 8,
    cash_equivalent: { foldInto: 'cash' },
    unclassified: 0, // The de-emphasis gray: a gap is not an identity.
  }

  /** The slice a class is drawn as, following any fold. */
  function bucketOf(value: string): AssetClass {
    const slot = CLASS_SLOT[value as AssetClass]
    if (slot === undefined) return 'unclassified'
    return typeof slot === 'object' ? slot.foldInto : (value as AssetClass)
  }

  function slotOf(key: AssetClass): number {
    const slot = CLASS_SLOT[key]
    return typeof slot === 'number' ? slot : 0
  }

  const CLASS_LABEL: Record<string, string> = {
    equity: 'Equity',
    fixed_income: 'Fixed income',
    cash: 'Cash',
    cash_equivalent: 'Cash equivalent',
    commodity: 'Commodity',
    crypto: 'Crypto',
    real_estate: 'Real estate',
    multi_asset: 'Multi-asset',
    alternative: 'Alternative',
    unclassified: 'Unclassified',
  }

  // A pie is part-to-whole, so every slice must be a non-negative share of one total. The
  // denominator is `assets_value` -- the balance sheet's asset side. `net_value` is already
  // reduced by debt, so slices over it would sum past 100%; debt belongs on the other side of
  // the sheet and is reported beneath the chart instead.
  //
  // `cash_by_currency` must NOT be filtered for positive buckets. consolidation.py folds every
  // member's cash into one bucket per currency, a liability account's negative balance included,
  // and reports the debt separately so `assets_value` can add it back. So a bucket reading
  // negative is a *netted* figure, not an overdrawn bank account: dropping it would erase real
  // deposits sharing that currency with a loan. Adding the debt back per currency restores the
  // asset-side balance, which is what a balance sheet calls cash.
  const SLICE_CAP = 6

  type Slice = {
    key: string
    label: string
    detail: string
    value: number
    share: number
    slot: number
  }

  const model = $derived.by(() => {
    // Only converted values can share one denominator. An unconverted holding is excluded
    // from `assets_value` too, so counting it here would invent a share of a total it is
    // not in -- it is surfaced as a count instead.
    const priced = summary.positions.filter((p) => p.reporting_market_value != null)
    const unpricedCount = summary.positions.length - priced.length

    const securities = priced.reduce((sum, p) => sum + Number(p.reporting_market_value), 0)
    const debt = Math.abs(Number(summary.liabilities_value)) || 0

    // The asset side of the sheet. `assets_value` is the server's own figure with the debt
    // already added back, so the cash it implies needs no reconstruction here -- and it can
    // never disagree with the NetWorth card, which reads the same field.
    const assets = Number(summary.assets_value)
    const cashTotal = Number.isFinite(assets) ? assets - securities : Number.NaN

    const buckets = summary.cash_by_currency
      .map((c) => ({ currency: c.currency, amount: Number(c.reporting_amount ?? Number.NaN) }))
      .filter((c) => Number.isFinite(c.amount))

    // Per-currency debt is not published, so a bucket that went negative cannot be un-netted on
    // its own. Where that happens the currencies are shown as one combined cash slice, since
    // splitting the debt across them would be a guess -- and a guessed split is indistinguishable
    // from a real one once drawn. The total is exact either way.
    const netted = buckets.some((c) => c.amount < 0)

    const cashEntries =
      !Number.isFinite(cashTotal) || cashTotal <= 0
        ? []
        : netted
          ? [
              {
                key: 'cash:all',
                label: 'Cash',
                detail:
                  buckets.length > 1 ? `${buckets.map((c) => c.currency).join(' + ')}` : 'Balance',
                value: cashTotal,
              },
            ]
          : buckets
              .filter((c) => c.amount > 0)
              .map((c) => ({
                key: `cash:${c.currency}`,
                label: `Cash ${c.currency}`,
                detail: 'Uninvested balance',
                value: c.amount,
              }))

    const holdingEntries = priced.map((p) => ({
      key: `${p.portfolio_id}:${p.ticker}`,
      label: p.ticker,
      detail: p.portfolio_name,
      value: Number(p.reporting_market_value),
    }))

    const entries = [...holdingEntries, ...cashEntries].filter(
      (e) => Number.isFinite(e.value) && e.value > 0,
    )

    const gross = entries.reduce((sum, e) => sum + e.value, 0)
    if (gross <= 0) return null

    const holdingCount = entries.length

    // Cash reaches the pie through the portfolio's currency, not through an instrument, so it
    // has no `asset_class` of its own -- it is folded in under the class it plainly is. Every
    // cash slice already carries the netting caveat computed above, which the by-class view
    // inherits unchanged rather than restating.
    const slices =
      mode === 'class'
        ? byClass(priced, cashEntries, gross)
        : byHolding(entries, gross)

    const unclassifiedValue =
      slices.find((s) => s.key === 'class:unclassified')?.value ?? 0

    return {
      slices,
      gross,
      holdingCount,
      debt,
      // Only worth explaining when a combined cash figure is actually on screen.
      netted: netted && cashEntries.length > 0,
      unpricedCount,
      unclassifiedValue,
    }
  })

  type Entry = { key: string; label: string; detail: string; value: number }

  /** Rank-coloured slices, tail folded: the identity that matters is the individual holding. */
  function byHolding(entries: Entry[], gross: number): Slice[] {
    const sorted = [...entries].sort((a, b) => b.value - a.value)

    // Past six segments a pie stops being readable at a glance, so the tail folds into one
    // de-emphasised bucket. Folding only pays when it removes more than the slice it costs.
    const fold = sorted.length > SLICE_CAP
    const head = fold ? sorted.slice(0, SLICE_CAP - 1) : sorted
    const tail = fold ? sorted.slice(SLICE_CAP - 1) : []

    const slices: Slice[] = head.map((e, i) => ({
      ...e,
      share: (e.value / gross) * 100,
      slot: i + 1,
    }))

    if (tail.length) {
      const value = tail.reduce((sum, e) => sum + e.value, 0)
      slices.push({
        key: 'other',
        label: 'Other',
        detail: `${tail.length} smaller holdings`,
        value,
        share: (value / gross) * 100,
        slot: 0, // slot 0 is the de-emphasis gray: "Other" is a remainder, not an identity.
      })
    }
    return slices
  }

  /** What a class slice is made of. Cash can hold a balance, cash-equivalent holdings, or both,
   *  so it says which rather than assuming -- every other class is only ever holdings. */
  function describe(key: string, tickers: number, hasBalance: boolean): string {
    const held = `${tickers} holding${tickers === 1 ? '' : 's'}`
    if (key !== 'cash') return held
    if (!tickers) return 'Uninvested balance'
    return hasBalance ? `Balance + ${held}` : held
  }

  /** Slices by economic exposure. No folding: the taxonomy is already few enough to read. */
  function byClass(
    priced: ConsolidatedSummary['positions'],
    cashEntries: Entry[],
    gross: number,
  ): Slice[] {
    const totals = new Map<AssetClass, { value: number; tickers: Set<string> }>()

    for (const position of priced) {
      const value = Number(position.reporting_market_value)
      if (!Number.isFinite(value) || value <= 0) continue
      const key = bucketOf(position.asset_class)
      const bucket = totals.get(key) ?? { value: 0, tickers: new Set<string>() }
      bucket.value += value
      bucket.tickers.add(position.ticker)
      totals.set(key, bucket)
    }

    const cashValue = cashEntries.reduce((sum, e) => sum + e.value, 0)
    const hasBalance = cashValue > 0
    if (hasBalance) {
      const bucket = totals.get('cash') ?? { value: 0, tickers: new Set<string>() }
      bucket.value += cashValue
      totals.set('cash', bucket)
    }

    return [...totals.entries()]
      .map(([key, bucket]) => ({
        key: `class:${key}`,
        label: CLASS_LABEL[key] ?? key.replace(/_/g, ' '),
        detail: describe(key, bucket.tickers.size, hasBalance),
        value: bucket.value,
        share: (bucket.value / gross) * 100,
        slot: slotOf(key),
      }))
      // Largest first, but `unclassified` is pinned last: it is a gap in the reading, not a
      // category competing with the others for attention.
      .sort((a, b) =>
        a.key === 'class:unclassified'
          ? 1
          : b.key === 'class:unclassified'
            ? -1
            : b.value - a.value,
      )
  }

  // Geometry. One shared circle description keeps the arcs, the gaps, and the hover ring
  // consistent -- a donut is drawn as a stroked circle so each segment is one dash run.
  const R = 60
  const CIRC = 2 * Math.PI * R
  // The 2px surface gap between segments, expressed in the stroke's own units.
  const GAP = 2

  const arcs = $derived.by(() => {
    if (!model) return []
    let offset = 0
    return model.slices.map((s) => {
      const length = (s.share / 100) * CIRC
      // A slice thinner than the gap would render as a gap-only sliver; floor it so every
      // slice the legend lists is visibly present on the ring.
      const dash = Math.max(length - GAP, 0.75)
      const arc = { ...s, dash, offset }
      offset += length
      return arc
    })
  })

  let active = $state<string | null>(null)

  const activeSlice = $derived(model?.slices.find((s) => s.key === active) ?? null)
</script>

<section class="card">
  <header>
    <h2>Asset allocation</h2>

    <div class="head-right">
      {#if model}
        <span class="faint">
          {model.holdingCount} holding{model.holdingCount === 1 ? '' : 's'}
        </span>
      {/if}

      <!-- Two readings of one total, not two charts: the geometry, palette, and legend are
           shared, so only the grouping changes underfoot. -->
      <div class="modes" role="group" aria-label="Group allocation by">
        <button
          class:on={mode === 'class'}
          aria-pressed={mode === 'class'}
          onclick={() => (mode = 'class')}
        >
          By type
        </button>
        <button
          class:on={mode === 'holding'}
          aria-pressed={mode === 'holding'}
          onclick={() => (mode = 'holding')}
        >
          By holding
        </button>
      </div>
    </div>
  </header>

  {#if !model}
    <p class="empty muted">No positive assets to allocate.</p>
  {:else}
    <div class="layout">
      <div class="plot">
        <svg
          viewBox="0 0 150 150"
          role="img"
          aria-label={mode === 'class'
            ? 'Asset allocation by asset type'
            : 'Asset allocation by holding'}
        >
          <g transform="rotate(-90 75 75)">
            {#each arcs as arc (arc.key)}
              <circle
                class="arc"
                class:dim={active !== null && active !== arc.key}
                cx="75"
                cy="75"
                r={R}
                fill="none"
                stroke="var(--slot-{arc.slot})"
                stroke-width={active === arc.key ? 20 : 16}
                stroke-dasharray="{arc.dash} {CIRC - arc.dash}"
                stroke-dashoffset={-arc.offset}
                role="presentation"
                onpointerenter={() => (active = arc.key)}
                onpointerleave={() => (active = null)}
              />
            {/each}
          </g>

          <!-- The hole carries the reading the chart is for: what the slices add up to. The
               total is compacted to fit the hole at any portfolio size; the exact figure is in
               the note below, so nothing is only readable here. -->
          <text class="hole-value" x="75" y="72" text-anchor="middle">
            {activeSlice
              ? percent(String(activeSlice.share))
              : compactMoney(String(model.gross), currency)}
          </text>
          <text class="hole-label" x="75" y="88" text-anchor="middle">
            {activeSlice ? activeSlice.label : 'Assets'}
          </text>
        </svg>
      </div>

      <!-- The legend is the table view: every slice's value is readable without hovering. -->
      <ul class="legend">
        {#each model.slices as slice (slice.key)}
          <li
            class:active={active === slice.key}
            onpointerenter={() => (active = slice.key)}
            onpointerleave={() => (active = null)}
          >
            <span class="dot" style="background: var(--slot-{slice.slot})"></span>
            <span class="label">
              {slice.label}
              <span class="detail faint">{slice.detail}</span>
            </span>
            <span class="values">
              <span class="num share">{percent(String(slice.share))}</span>
              <span class="num amount faint">{money(String(slice.value), currency)}</span>
            </span>
          </li>
        {/each}
      </ul>
    </div>

      <ul class="notes muted">
        <li>
          {#if model.debt > 0}
            Shares are of {money(String(model.gross), currency)} in assets, not of net worth:
            {money(String(model.debt), currency)} of debt sits on the other side of the balance
            sheet and is not a slice.
          {:else}
            Shares are of {money(String(model.gross), currency)} in assets.
          {/if}
        </li>
        {#if model.netted}
          <li>
            Cash is shown as one figure because a loan shares a currency with the bank accounts:
            the balances net before they are reported, so the per-currency split cannot be
            recovered. The total is exact.
          </li>
        {/if}
        {#if model.unpricedCount > 0}
          <li>
            {model.unpricedCount} holding{model.unpricedCount === 1 ? '' : 's'} could not be
            converted to {currency} and {model.unpricedCount === 1 ? 'is' : 'are'} excluded.
          </li>
        {/if}
        <!-- Actionable by construction: the note appears only while a gap exists, names what it
             is worth, and links to the page that closes it. Once nothing is unclassified it
             disappears rather than becoming a warning nobody can clear. -->
        {#if mode === 'class' && model.unclassifiedValue > 0}
          <li>
            {money(String(model.unclassifiedValue), currency)} sits in holdings whose asset class
            nobody has resolved, so it is shown as its own slice rather than assumed.
            <a href="#/classify">Classify them</a> to see it split.
          </li>
        {/if}
      </ul>
  {/if}
</section>

<style>
  /* Categorical slots, validated with the dataviz palette validator against this dashboard's
     own surfaces (#ffffff light, #171b21 dark), adjacent pairlist, both modes. All eight
     identity slots pass; the three light-mode slots under 3:1 contrast take the relief rule,
     satisfied by the legend, which carries every slice's name and value as text. Slot 0 is the
     de-emphasis gray for "Other" and "Unclassified": neither is an identity, so it deliberately
     sits below the chroma floor the identity slots must clear.

     Eight is the whole palette -- there is no ninth hue to invent, which is why the by-type
     view folds `cash_equivalent` into `cash` rather than growing this list. */
  .card {
    --slot-1: #2a78d6;
    --slot-2: #eb6834;
    --slot-3: #1baf7a;
    --slot-4: #eda100;
    --slot-5: #e87ba4;
    --slot-6: #008300;
    --slot-7: #4a3aa7;
    --slot-8: #e34948;
    --slot-0: #8a939f;

    padding: var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  :root:not([data-theme='light']) {
    @media (prefers-color-scheme: dark) {
      .card {
        --slot-1: #3987e5;
        --slot-2: #d95926;
        --slot-3: #199e70;
        --slot-4: #c98500;
        --slot-5: #d55181;
        --slot-6: #008300;
        --slot-7: #9085e9;
        --slot-8: #e66767;
        --slot-0: #6f7a87;
      }
    }
  }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }

  h2 {
    font-size: 15px;
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .modes {
    display: flex;
    gap: 2px;
    padding: 2px;
    background: var(--surface-sunken);
    border-radius: var(--radius-sm);
  }

  .modes button {
    padding: 4px 10px;
    font: inherit;
    font-size: 12px;
    color: var(--text-faint);
    background: none;
    border: none;
    border-radius: calc(var(--radius-sm) - 1px);
    cursor: pointer;
  }

  .modes button.on {
    color: var(--text);
    background: var(--surface);
  }

  .notes a {
    color: inherit;
  }

  .layout {
    display: flex;
    align-items: center;
    gap: 32px;
  }

  .plot {
    flex: 0 0 190px;
  }

  svg {
    width: 190px;
    height: 190px;
    display: block;
  }

  /* The legend carries the values, so it is capped rather than stretched: a row whose name and
     figures sit at opposite ends of a wide card is two columns the eye has to reconnect. */
  .legend {
    max-width: 620px;
  }

  .arc {
    transition:
      stroke-width 120ms ease-out,
      opacity 120ms ease-out;
  }

  /* Hovering one slice recedes the others rather than repainting them: colour follows the
     holding, never its current state. */
  .arc.dim {
    opacity: 0.35;
  }

  /* Large standalone figures take proportional digits; tabular only aligns columns. */
  .hole-value {
    font-size: 15px;
    font-weight: 600;
    fill: var(--text);
  }

  .hole-label {
    font-size: 10px;
    fill: var(--text-faint);
  }

  .legend {
    flex: 1;
    list-style: none;
    margin: 0;
    padding: 0;
    font-size: 13px;
    min-width: 0;
  }

  .legend li {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 5px 6px;
    border-radius: var(--radius-sm);
  }

  .legend li.active {
    background: var(--surface-sunken);
  }

  .dot {
    flex: 0 0 auto;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    transform: translateY(1px);
  }

  .label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .detail {
    margin-left: 6px;
    font-size: 11px;
  }

  .values {
    margin-left: auto;
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex: 0 0 auto;
  }

  .share {
    font-size: 12px;
  }

  .amount {
    font-size: 11px;
  }

  .notes {
    list-style: none;
    margin: 14px 0 0;
    padding: 12px 0 0;
    border-top: 1px solid var(--border);
    font-size: 12px;
  }

  .notes li + li {
    margin-top: 4px;
  }

  .empty {
    padding: 16px 0;
  }

  /* The legend needs room for a name and two figures; below this it reads better stacked. */
  @media (max-width: 520px) {
    .layout {
      flex-direction: column;
      align-items: stretch;
    }

    .plot {
      align-self: center;
    }

    .amount {
      display: none;
    }
  }
</style>
