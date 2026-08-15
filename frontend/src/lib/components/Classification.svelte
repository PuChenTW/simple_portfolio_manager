<script lang="ts">
  import { api, ApiError, ASSET_CLASSES, type AssetClass, type ConsolidatedSummary } from '../api/client'
  import { money, percent } from '../format'

  let { summary, onchange }: { summary: ConsolidatedSummary; onchange?: () => void } = $props()

  const currency = $derived(summary.reporting_currency)

  type Row = {
    key: string
    ticker: string
    /** Null when the instrument has no stable id: nothing can be classified without one. */
    reference: string | null
    assetClass: AssetClass
    provenance: string
    value: number | null
    share: number | null
    resolved: boolean
    overridden: boolean
  }

  // One ticker can be held in several accounts, but an asset class belongs to the *instrument*,
  // not to the holding -- classifying it twice is how the two copies come to disagree. Rows are
  // folded by ticker and their values summed, so the share column reads as total exposure.
  const rows = $derived.by(() => {
    const assets = Number(summary.assets_value)
    const byTicker = new Map<string, Row>()

    for (const position of summary.positions) {
      const existing = byTicker.get(position.ticker)
      const value = position.reporting_market_value
        ? Number(position.reporting_market_value)
        : null

      if (existing) {
        // A holding the server could not convert contributes no value; it must not turn a real
        // total into NaN, and it must not read as zero either.
        if (value !== null) existing.value = (existing.value ?? 0) + value
        continue
      }

      byTicker.set(position.ticker, {
        key: position.ticker,
        ticker: position.ticker,
        reference: position.instrument_id ?? null,
        assetClass: position.asset_class,
        provenance: position.asset_class_provenance,
        value,
        share: null,
        resolved: position.asset_class !== 'unclassified',
        overridden: position.asset_class_provenance === 'manual_override',
      })
    }

    const list = [...byTicker.values()]
    for (const row of list) {
      row.share =
        row.value !== null && Number.isFinite(assets) && assets > 0
          ? (row.value / assets) * 100
          : null
    }
    // Largest first: the holding that moves the allocation most is the one worth resolving first.
    list.sort((a, b) => (b.value ?? -1) - (a.value ?? -1))
    return list
  })

  const pending = $derived(rows.filter((row) => !row.resolved))
  const done = $derived(rows.filter((row) => row.resolved))

  // What the gap is actually worth. A count alone cannot say whether 9 unclassified holdings are
  // a rounding error or most of the book.
  const pendingShare = $derived(
    pending.reduce((sum, row) => sum + (row.share ?? 0), 0),
  )

  /** Draft state per ticker, so an in-progress edit survives re-renders of the list. */
  let choice = $state<Record<string, AssetClass | ''>>({})
  let reason = $state<Record<string, string>>({})
  let saving = $state<string | null>(null)
  let failed = $state<Record<string, string>>({})
  let expanded = $state(false)

  function canSave(row: Row): boolean {
    return (
      row.reference !== null &&
      !!choice[row.key] &&
      reason[row.key]?.trim().length > 0 &&
      saving !== row.key
    )
  }

  async function save(row: Row): Promise<void> {
    const value = choice[row.key]
    const why = reason[row.key]?.trim()
    if (!row.reference || !value || !why) return

    saving = row.key
    const { [row.key]: _dropped, ...rest } = failed
    failed = rest
    try {
      await api.setAssetClass(row.reference, { value, reason: why })
      choice = { ...choice, [row.key]: '' }
      reason = { ...reason, [row.key]: '' }
      // The summary is the source of truth for what is now resolved; refetch rather than
      // patching a local copy, so the page can never disagree with the API about a saved value.
      onchange?.()
    } catch (err) {
      failed = {
        ...failed,
        [row.key]: err instanceof ApiError ? err.message : String(err),
      }
    } finally {
      saving = null
    }
  }

  async function retract(row: Row): Promise<void> {
    if (!row.reference) return
    saving = row.key
    try {
      await api.setAssetClass(row.reference, { reason: 'retracted from dashboard', retract: true })
      onchange?.()
    } catch (err) {
      failed = {
        ...failed,
        [row.key]: err instanceof ApiError ? err.message : String(err),
      }
    } finally {
      saving = null
    }
  }

  function label(value: string): string {
    return value.replace(/_/g, ' ')
  }
</script>

<section class="card">
  <header>
    <h2>Classification</h2>
    <span class="faint">
      {#if pending.length}
        {pending.length} unclassified · {percent(String(pendingShare))} of assets
      {:else}
        every holding classified
      {/if}
    </span>
  </header>

  <p class="lead muted">
    A data provider reports what an instrument <em>is</em> — an ETF, a common stock — but never
    what a fund <em>holds</em>. That is why every fund below arrives unclassified: reading them
    all as equity would file gold and bond funds under stocks and leave nothing to show the
    error. Setting a value here outranks the provider without overwriting it, so retracting
    always restores the original.
  </p>

  {#if pending.length}
    <ul class="rows">
      {#each pending as row (row.key)}
        <li class:busy={saving === row.key}>
          <div class="ident">
            <span class="ticker">{row.ticker}</span>
            <span class="weight faint">
              {row.share !== null ? percent(String(row.share)) : '—'}
              <span class="amount">{money(row.value?.toString() ?? null, currency)}</span>
            </span>
          </div>

          {#if row.reference === null}
            <p class="note muted">
              No stable instrument id, so there is nothing to attach a classification to.
            </p>
          {:else}
            <div class="edit">
              <label>
                <span class="visually-hidden">Asset class for {row.ticker}</span>
                <select bind:value={choice[row.key]} disabled={saving === row.key}>
                  <option value="">Asset class…</option>
                  {#each ASSET_CLASSES.filter((c) => c !== 'unclassified') as option (option)}
                    <option value={option}>{label(option)}</option>
                  {/each}
                </select>
              </label>

              <label class="grow">
                <span class="visually-hidden">Reason for {row.ticker}</span>
                <input
                  type="text"
                  placeholder="Why — kept for audit"
                  bind:value={reason[row.key]}
                  disabled={saving === row.key}
                />
              </label>

              <button onclick={() => save(row)} disabled={!canSave(row)}>
                {saving === row.key ? 'Saving…' : 'Save'}
              </button>
            </div>
          {/if}

          {#if failed[row.key]}
            <p class="failed">{failed[row.key]}</p>
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <p class="empty muted">Nothing left to classify.</p>
  {/if}

  {#if done.length}
    <div class="done">
      <button class="toggle" onclick={() => (expanded = !expanded)} aria-expanded={expanded}>
        {expanded ? '▾' : '▸'} Classified ({done.length})
      </button>

      {#if expanded}
        <ul class="rows compact">
          {#each done as row (row.key)}
            <li class:busy={saving === row.key}>
              <div class="ident">
                <span class="ticker">{row.ticker}</span>
                <span class="resolved">
                  {label(row.assetClass)}
                  <!-- Provenance is shown because a provider's guess and a verified decision are
                       not equally trustworthy, and only one of them is safe to leave alone. -->
                  <span class="prov faint">{label(row.provenance)}</span>
                </span>
                <span class="weight faint">
                  {row.share !== null ? percent(String(row.share)) : '—'}
                </span>
              </div>

              {#if row.reference !== null}
                <div class="edit">
                  <label>
                    <span class="visually-hidden">Change asset class for {row.ticker}</span>
                    <select bind:value={choice[row.key]} disabled={saving === row.key}>
                      <option value="">Change…</option>
                      {#each ASSET_CLASSES.filter((c) => c !== 'unclassified') as option (option)}
                        <option value={option}>{label(option)}</option>
                      {/each}
                    </select>
                  </label>

                  <label class="grow">
                    <span class="visually-hidden">Reason for {row.ticker}</span>
                    <input
                      type="text"
                      placeholder="Why — kept for audit"
                      bind:value={reason[row.key]}
                      disabled={saving === row.key}
                    />
                  </label>

                  <button onclick={() => save(row)} disabled={!canSave(row)}>Save</button>

                  {#if row.overridden}
                    <button
                      class="ghost"
                      onclick={() => retract(row)}
                      disabled={saving === row.key}
                      title="Restore the provider's value"
                    >
                      Retract
                    </button>
                  {/if}
                </div>
              {/if}

              {#if failed[row.key]}
                <p class="failed">{failed[row.key]}</p>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
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
    gap: 12px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }

  h2 {
    font-size: 15px;
  }

  .lead {
    margin: 0 0 16px;
    font-size: 12px;
    max-width: 68ch;
    line-height: 1.5;
  }

  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .rows li {
    padding: 10px 0;
    border-top: 1px solid var(--border);
  }

  .rows li.busy {
    opacity: 0.55;
  }

  .ident {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }

  .ticker {
    font-weight: 600;
    font-size: 13px;
  }

  .weight {
    margin-left: auto;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }

  .amount {
    margin-left: 8px;
    font-size: 11px;
  }

  .resolved {
    font-size: 12px;
  }

  .prov {
    margin-left: 6px;
    font-size: 11px;
  }

  .edit {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .grow {
    flex: 1;
    min-width: 180px;
  }

  select,
  input {
    width: 100%;
    padding: 6px 9px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
  }

  select {
    min-width: 150px;
  }

  button {
    padding: 6px 14px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface-sunken);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .ghost {
    background: transparent;
  }

  .toggle {
    border: none;
    background: none;
    padding: 0;
    font-size: 12px;
    color: var(--text-faint);
  }

  .done {
    margin-top: 18px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
  }

  .compact li:first-child {
    border-top: none;
  }

  .failed {
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--negative);
  }

  .note,
  .empty {
    margin: 0;
    font-size: 12px;
  }

  .empty {
    padding: 12px 0;
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }

  /* The edit row needs a select, a text field, and a button side by side; below this the text
     field is squeezed to uselessness, so the controls stack. */
  @media (max-width: 620px) {
    .edit {
      flex-direction: column;
      align-items: stretch;
    }

    .amount {
      display: none;
    }
  }
</style>
