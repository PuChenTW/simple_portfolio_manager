<script lang="ts">
  import { api, ApiError, type Portfolio, type Rebuild } from '../../api/client'
  import { addDays, dayCount, monthChunks, startOfYear, today } from '../../snapshots'

  let { portfolio }: { portfolio: Portfolio } = $props()

  type Preset = 'recent' | 'year' | 'all'

  let preset = $state<Preset>('recent')
  let force = $state(false)
  /** Set when the force checkbox is on and the user has clicked once. The second click runs. */
  let confirmingForce = $state(false)

  let coverage = $state<{ stored: number; missing: number; partial: number } | null>(null)
  let coverageError = $state<string | null>(null)
  let loadingCoverage = $state(false)

  let running = $state(false)
  let progress = $state<{ done: number; total: number; label: string } | null>(null)
  let result = $state<{
    created: number
    skipped: number
    partial: number
    failedChunks: string[]
    // Dates and warnings stay verbatim. A count of warnings is not actionable, and a warning
    // nobody can act on is the failure AGENTS.md names: readers learn to ignore all of them.
    failedDates: string[]
    warnings: string[]
  } | null>(null)

  const end = $derived(today())

  const start = $derived.by(() => {
    if (preset === 'recent') return addDays(end, -29)
    if (preset === 'year') return startOfYear(end)
    return portfolio.first_event_date ?? ''
  })

  /** "All history" needs a first event. A book with none has no history to build. */
  const hasHistory = $derived(portfolio.first_event_date != null)
  const rangeValid = $derived(start !== '' && dayCount(start, end) > 0)
  const chunks = $derived(rangeValid ? monthChunks(start, end) : [])

  // Reload coverage whenever the account or the chosen range changes, so every count on screen
  // describes the range the buttons would act on.
  $effect(() => {
    const [from, to] = [start, end]
    const portfolioId = portfolio.id
    if (!rangeValid) {
      coverage = null
      coverageError = null
      return
    }

    let cancelled = false
    loadingCoverage = true
    coverageError = null
    api
      .navHistory(portfolioId, from, to)
      .then((history) => {
        if (cancelled) return
        coverage = {
          stored: history.snapshots.length,
          missing: history.missing_dates.length,
          partial: history.partial_snapshots,
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        coverage = null
        coverageError = err instanceof Error ? err.message : String(err)
      })
      .finally(() => {
        if (!cancelled) loadingCoverage = false
      })

    return () => {
      cancelled = true
    }
  })

  // Switching account or range retracts a pending confirmation, so a click armed for one range
  // can never fire against another.
  $effect(() => {
    void portfolio.id
    void start
    void force
    confirmingForce = false
    result = null
  })

  async function run(): Promise<void> {
    if (!rangeValid || running) return
    if (force && !confirmingForce) {
      confirmingForce = true
      return
    }

    running = true
    confirmingForce = false
    result = null
    const totals = {
      created: 0,
      skipped: 0,
      partial: 0,
      failedChunks: [] as string[],
      failedDates: [] as string[],
      warnings: [] as string[],
    }

    for (const [index, chunk] of chunks.entries()) {
      progress = { done: index, total: chunks.length, label: chunk.label }
      try {
        const report: Rebuild = await api.rebuildSnapshots(
          portfolio.id,
          chunk.start,
          chunk.end,
          force,
        )
        totals.created += report.created
        totals.skipped += report.skipped_existing
        totals.partial += report.partial
        totals.failedDates.push(...report.failed)
        totals.warnings.push(...report.warnings)
      } catch (err) {
        // One failed month must not abandon the rest, mirroring what the endpoint already does
        // for one bad date. Every chunk is independently re-runnable, so clicking again picks
        // up only what is still missing.
        const reason = err instanceof ApiError ? err.message : String(err)
        totals.failedChunks.push(`${chunk.label}: ${reason}`)
      }
    }

    progress = null
    running = false
    result = {
      ...totals,
      warnings: [...new Set(totals.warnings)],
      failedDates: [...new Set(totals.failedDates)],
    }

    // The counts on screen described the range before the run. Re-read them.
    const history = await api.navHistory(portfolio.id, start, end).catch(() => null)
    if (history) {
      coverage = {
        stored: history.snapshots.length,
        missing: history.missing_dates.length,
        partial: history.partial_snapshots,
      }
    }
  }

  const actionLabel = $derived.by(() => {
    if (running) return 'Building…'
    if (force) return confirmingForce ? `Replace ${coverage?.stored ?? 0} — confirm` : 'Replace snapshots'
    if (coverage && coverage.missing === 0) return 'Nothing missing'
    return `Build ${coverage?.missing ?? 0} missing snapshots`
  })

  const canRun = $derived(
    rangeValid && !running && !loadingCoverage && (force || (coverage?.missing ?? 0) > 0),
  )
</script>

<section class="card">
  <header><h2>Valuation snapshots</h2></header>

  <p class="note muted">
    A snapshot values this account on one date, replaying the journal to that date and pricing it
    with history bounded by it. The daily job keeps recent dates filled. Build them here to reach
    further back than that window, or to replace dates a correction made wrong.
  </p>

  <div class="presets" role="group" aria-label="Range">
    <label class:selected={preset === 'recent'}>
      <input type="radio" bind:group={preset} value="recent" />
      Last 30 days
    </label>
    <label class:selected={preset === 'year'}>
      <input type="radio" bind:group={preset} value="year" />
      This year
    </label>
    <label class:selected={preset === 'all'} class:disabled={!hasHistory}>
      <input type="radio" bind:group={preset} value="all" disabled={!hasHistory} />
      All history
    </label>
  </div>

  {#if preset === 'all' && !hasHistory}
    <p class="note muted">This account has no transactions yet, so there is no history to build.</p>
  {:else if rangeValid}
    <p class="range muted">
      {start} to {end} · {chunks.length}
      {chunks.length === 1 ? 'request' : 'requests'}, one per month
    </p>
  {/if}

  {#if coverageError}
    <p class="note negative">Could not read coverage: {coverageError}</p>
  {:else if coverage}
    <dl class="facts">
      <div><dt>Stored</dt><dd class="num">{coverage.stored}</dd></div>
      <div><dt>Missing</dt><dd class="num">{coverage.missing}</dd></div>
      <div><dt>Partial</dt><dd class="num">{coverage.partial}</dd></div>
    </dl>
    {#if coverage.partial > 0}
      <p class="note muted">
        A partial snapshot priced some holdings and not others. Rebuilding helps only if the
        missing prices are now available.
      </p>
    {/if}
  {/if}

  <label class="check">
    <input type="checkbox" bind:checked={force} disabled={running} />
    <span>Replace snapshots that already exist</span>
  </label>

  {#if force}
    <p class="note warn">
      Rewrites stored valuations for every date in the range, not only the missing ones. Use this
      after a reversal or a corrected trade made the recorded values wrong.
    </p>
  {/if}

  <div class="actions">
    <button
      type="button"
      class={force ? 'destructive' : 'primary'}
      disabled={!canRun}
      onclick={run}
    >
      {actionLabel}
    </button>
    {#if confirmingForce}
      <button type="button" class="plain" onclick={() => (confirmingForce = false)}>Cancel</button>
    {/if}
    {#if progress}
      <span class="progress">{progress.label} · {progress.done + 1} of {progress.total}</span>
    {/if}
  </div>

  {#if progress}
    <div class="bar" role="progressbar" aria-valuenow={progress.done} aria-valuemin="0" aria-valuemax={progress.total}>
      <span style:width={`${(progress.done / progress.total) * 100}%`}></span>
    </div>
  {/if}

  {#if result}
    <div class="result">
      <p class="note">
        Created {result.created} · skipped {result.skipped} · partial {result.partial}
      </p>

      {#if result.failedChunks.length > 0}
        <p class="note negative">These months failed. Running again retries only what is missing.</p>
        <ul class="issues">
          {#each result.failedChunks as failure (failure)}<li>{failure}</li>{/each}
        </ul>
      {/if}

      {#if result.failedDates.length > 0}
        <p class="note negative">Dates that could not be valued:</p>
        <ul class="issues">
          {#each result.failedDates as date (date)}<li>{date}</li>{/each}
        </ul>
      {/if}

      {#if result.warnings.length > 0}
        <ul class="issues muted">
          {#each result.warnings as warning (warning)}<li>{warning}</li>{/each}
        </ul>
      {/if}
    </div>
  {/if}
</section>

<style>
  .card {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  header {
    margin-bottom: 0;
  }

  h2 {
    font-size: 15px;
  }

  .note {
    margin: 0;
    font-size: 13px;
  }

  .range {
    margin: 0;
    font-size: 12px;
  }

  .presets {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .presets label {
    padding: 6px 12px;
    font-size: 13px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .presets label.selected {
    color: #fff;
    background: var(--accent);
    border-color: var(--accent);
  }

  .presets label.disabled {
    opacity: 0.5;
    cursor: default;
  }

  .presets input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .presets label:focus-within {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
  }

  .warn {
    color: var(--negative);
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
    gap: 12px 24px;
    margin: 0;
  }

  .facts div {
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
    font-size: 14px;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 13px;
  }

  button {
    padding: 7px 14px;
    font: inherit;
    font-size: 13px;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .primary {
    color: #fff;
    background: var(--accent);
    border: 1px solid var(--accent);
  }

  .destructive {
    color: var(--negative);
    background: var(--surface);
    border: 1px solid var(--negative);
  }

  .destructive:not(:disabled):hover {
    color: #fff;
    background: var(--negative);
  }

  .plain {
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
  }

  .progress {
    font-size: 12px;
    color: var(--text-muted);
  }

  .bar {
    height: 4px;
    overflow: hidden;
    background: var(--border);
    border-radius: 2px;
  }

  .bar span {
    display: block;
    height: 100%;
    background: var(--accent);
    transition: width 120ms linear;
  }

  .result {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 4px;
    border-top: 1px solid var(--border);
  }

  .issues {
    margin: 0;
    padding-left: 18px;
    font-size: 12px;
  }

  .issues li {
    word-break: break-word;
  }
</style>
