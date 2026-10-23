<script lang="ts">
  import { tick } from 'svelte'
  import { api, type Portfolio } from '../../api/client'
  import {
    errorFor,
    prepareEntry,
    staleFrom,
    TYPE_LABELS,
    typesForKind,
    type ErrorField,
    type TransactionType,
  } from '../../transactions'
  import { exactMoney, shortDate } from '../../format'
  import { today } from '../../snapshots'
  import { timezone } from '../../timezone.svelte'
  import { localTimeOptions, timeFields } from '../../timezone'
  import StaleSnapshots from './StaleSnapshots.svelte'

  let {
    portfolio,
    onposted,
  }: {
    portfolio: Portfolio
    /** A posting landed. The page reloads the ledger and drops the stale summary. */
    onposted: () => Promise<void>
  } = $props()

  let open = $state(false)

  const types = $derived(typesForKind(portfolio.kind))
  let type = $state<TransactionType>('deposit')

  // Fields, all strings. An amount never becomes a JS number on its way to the wire: parsing
  // "0.1" and re-serializing it yields 0.30000000000000004 once it is added to anything, and the
  // ledger stores whatever arrives. Only the preview parses, and only to show a human a figure.
  let amount = $state('')
  let memo = $state('')
  let ticker = $state('')
  let quantity = $state('')
  let unitPrice = $state('')
  let fee = $state('')
  let tax = $state('')

  // Both exist for entering from a statement, which is the main reason to post by hand at all.
  // Collapsed by default because ordinary entry needs neither, and because a wrong
  // `settlement_amount` unbalances the event rather than merely mis-stating it.
  let advancedOpen = $state(false)
  let settlementAmount = $state('')
  let sourceReference = $state('')

  /** What the last blur resolved, so a stale name never sits beside a since-edited symbol. */
  let resolved = $state<{ symbol: string; name: string; currency: string } | null>(null)
  let resolving = $state(false)
  let unresolved = $state<string | null>(null)

  /** Resolve the symbol and report what it is, without ever blocking submit.
   *
   * The name is the point: a typo that resolves to a real but wrong company is the failure the
   * server cannot catch, because both symbols are valid. The currency is the second point --
   * comparing it here turns a post-submit `currency_mismatch` 422 into a pre-submit fact, out of
   * a request that is already being made.
   *
   * Both warnings are advisory. An unresolvable symbol may simply be delisted, which
   * `_resolve_or_fetch` cannot seed, and refusing to post it would make this form stricter than
   * the ledger it writes to. The server stays the authority.
   */
  async function resolveTicker(): Promise<void> {
    const symbol = ticker.trim().toUpperCase()
    resolved = null
    unresolved = null
    if (!symbol) return

    resolving = true
    try {
      const instrument = await api.marketInstrument(symbol)
      // Discard a response for a symbol the user has already edited past.
      if (ticker.trim().toUpperCase() !== symbol) return
      resolved = { symbol, name: instrument.name, currency: instrument.currency }
    } catch {
      if (ticker.trim().toUpperCase() === symbol) unresolved = symbol
    } finally {
      resolving = false
    }
  }

  const currencyWarning = $derived(
    resolved && resolved.currency !== portfolio.base_currency
      ? `${resolved.symbol} trades in ${resolved.currency}; this account is ${portfolio.base_currency}.`
      : null,
  )

  let occurredOn = $state('')
  let occurredTime = $state('')
  let entryTimezone = $state(timezone.effective)
  let offset = $state('')
  const timeOptions = $derived(localTimeOptions({
    date: occurredOn, time: occurredTime, timezone: entryTimezone,
  }))

  function setNow(): void {
    const fields = timeFields(entryTimezone)
    occurredOn = fields.date
    occurredTime = fields.time
    offset = ''
    clearConfirmation()
  }

  /** One idempotency key per form session, re-minted only after a success.
   *
   * A double-clicked submit sends the same id twice. The server fingerprints the payload against
   * it, matches, and returns the event it already wrote -- one posting, not two. A *failed*
   * submit keeps the key, so the retry is the same logical mutation rather than a second one.
   */
  let requestId = $state(crypto.randomUUID())

  const entry = $derived(prepareEntry({
    type, amount, ticker, quantity, unitPrice, fee, tax, settlementAmount,
    occurredOn, occurredTime, timezone: entryTimezone, offset, requestId, memo, sourceReference,
  }, portfolio.kind))
  const shape = $derived(entry.shape)
  const showTicker = $derived(entry.showTicker)
  const effect = $derived(entry.effect)

  let submitting = $state(false)
  let error = $state<{ field: ErrorField; message: string } | null>(null)
  /** The date of the last successful posting, or null once nothing needs saying about it. */
  let postedOn = $state<string | null>(null)

  function fieldError(field: ErrorField): string | null {
    return error && error.field === field ? error.message : null
  }

  /** Drop the last posting's confirmation once this entry stops being that posting.
   *
   * "Posted." and the stale-snapshot notice both describe the entry that just landed. Left up
   * while the user starts a different one, the notice names a date no longer on screen -- which
   * is the "warning nobody can act on" AGENTS.md warns teaches readers to ignore the rest. */
  function clearConfirmation(): void {
    postedOn = null
  }

  function toggle(): void {
    open = !open
    if (open) reset(true)
  }

  /** Clear the entry fields. `full` also resets what a burst of entries should carry over. */
  function reset(full: boolean): void {
    amount = ''
    memo = ''
    ticker = ''
    quantity = ''
    unitPrice = ''
    fee = ''
    tax = ''
    settlementAmount = ''
    sourceReference = ''
    resolved = null
    unresolved = null
    error = null
    requestId = crypto.randomUUID()
    if (full) {
      type = types.includes('deposit') ? 'deposit' : types[0]
      entryTimezone = timezone.effective
      setNow()
      postedOn = null
    }
  }

  const canSubmit = $derived(!submitting && entry.complete)

  /** A back-dated posting invalidates every snapshot from its date forward.
   *
   * Saying so is the whole point: without it the user gets a correct ledger and a performance
   * chart that still reports the old numbers, with nothing on screen connecting the two. It does
   * not rebuild automatically -- a force rebuild replaces stored valuations, which is why
   * `AccountSnapshots` gates it behind a second click naming how many.
   */
  const staleDate = $derived(postedOn ? staleFrom(postedOn, today()) : null)

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault()
    if (!canSubmit) return
    if (new Date(entry.payload.occurred_at!).getTime() > Date.now()) {
      error = { field: null, message: 'The transaction time must not be in the future.' }
      return
    }

    submitting = true
    error = null
    try {
      await api.recordTransaction(portfolio.id, entry.payload)
      postedOn = occurredOn
      // Type and date survive: the next line of a statement usually shares both.
      reset(false)
      await onposted()
      // `tick()` because `reset` re-renders the fields: without it the focus call lands on the
      // node Svelte is about to replace, and a burst of statement entries needs the mouse again.
      await tick()
      firstField()?.focus()
    } catch (err) {
      error = errorFor(err)
      // A refusal shown inside a collapsed disclosure is a refusal nobody sees.
      if (error.field === 'settlement_amount') advancedOpen = true
    } finally {
      submitting = false
    }
  }

  // Each shape's first box. Both are needed because only one is rendered at a time: a single
  // binding on the amount input is `null` on the trade shape -- the shape with the most fields
  // to fill, and so the one that needs the focus return most.
  let amountInput: HTMLInputElement | null = $state(null)
  let tickerInput: HTMLInputElement | null = $state(null)

  /** The first field of whichever shape is showing, so a burst of entries never needs a mouse. */
  function firstField(): HTMLInputElement | null {
    return shape === 'trade' ? tickerInput : amountInput
  }

</script>

<section class="card">
  <header>
    <h2>Record transaction</h2>
    <button type="button" class="toggle" aria-expanded={open} onclick={toggle}>
      {open ? 'Close' : 'New entry'}
    </button>
  </header>

  {#if open}
    <form onsubmit={submit}>
      <!-- The type comes first because it decides every field below it. -->
      <label class="field">
        <span class="label">Type</span>
        <select
          bind:value={type}
          onchange={() => {
            error = null
            clearConfirmation()
          }}
        >
          {#each types as option (option)}
            <option value={option}>{TYPE_LABELS[option]}</option>
          {/each}
        </select>
      </label>

      <div class="row">
        <label class="field">
          <span class="label">Date</span>
          <input type="date" bind:value={occurredOn} onchange={() => { offset = ''; clearConfirmation() }} required />
          <span class="hint faint">The date the transaction happened, not today.</span>
        </label>

        <label class="field">
          <span class="label">Time ({entryTimezone})</span>
          <input type="time" step="60" bind:value={occurredTime}
            onchange={() => { offset = ''; clearConfirmation() }} required />
        </label>
        <button type="button" onclick={setNow}>Set to now</button>
        {#if timeOptions.length > 1}
          <label class="field">
            <span class="label">UTC offset</span>
            <select bind:value={offset} required>
              <option value="">Choose offset</option>
              {#each timeOptions as option}
                <option value={option.offset}>{option.offset}</option>
              {/each}
            </select>
          </label>
        {/if}
        {#if entry.timeError}<p class="error" role="alert">{entry.timeError}</p>{/if}

        {#if shape !== 'trade'}
          <label class="field">
            <span class="label">
              {shape === 'income' ? 'Gross amount' : 'Amount'} ({portfolio.base_currency})
            </span>
            <!-- `inputmode` rather than `type="number"`: a number input hands back a JS number
                 and rounds what it cannot represent. The value stays a string end to end. -->
            <input
              bind:this={amountInput}
              type="text"
              inputmode="decimal"
              bind:value={amount}
              placeholder="0.00"
              aria-invalid={!!fieldError('amount')}
              required
            />
            <span class="hint faint">
              A positive figure. The type above decides which way the money moves.
            </span>
            {#if fieldError('amount')}
              <span class="negative msg">{fieldError('amount')}</span>
            {/if}
          </label>
        {/if}
      </div>

      {#if showTicker}
        <label class="field wide">
          <span class="label">
            Instrument {#if shape !== 'trade'}<span class="faint">(optional)</span>{/if}
          </span>
          <input
            bind:this={tickerInput}
            type="text"
            bind:value={ticker}
            onblur={resolveTicker}
            placeholder="AAPL"
            autocapitalize="characters"
            spellcheck="false"
            aria-invalid={!!fieldError('ticker')}
          />
          <!-- Every line below is advisory. The name confirms the symbol is the company the
               user meant, which is the one mistake the server cannot catch: a typo that
               resolves to a real but different company posts cleanly. -->
          {#if resolving}
            <span class="hint faint">Resolving…</span>
          {:else if resolved}
            <span class="hint faint">{resolved.name} · {resolved.currency}</span>
          {:else if unresolved}
            <span class="hint warn">
              {unresolved} could not be resolved. It may be delisted or misspelled — posting will
              still be attempted.
            </span>
          {/if}
          {#if currencyWarning}
            <span class="hint warn">{currencyWarning}</span>
          {/if}
          {#if fieldError('ticker')}
            <span class="negative msg">{fieldError('ticker')}</span>
          {/if}
        </label>
      {/if}

      {#if shape === 'trade'}
        <div class="row">
          <label class="field">
            <span class="label">Quantity</span>
            <input
              type="text"
              inputmode="decimal"
              bind:value={quantity}
              placeholder="0"
              aria-invalid={!!fieldError('quantity')}
              required
            />
            {#if fieldError('quantity')}
              <span class="negative msg">{fieldError('quantity')}</span>
            {/if}
          </label>

          <label class="field">
            <span class="label">Unit price ({portfolio.base_currency})</span>
            <input type="text" inputmode="decimal" bind:value={unitPrice} placeholder="0.00" required />
            <span class="hint faint">The price actually executed, not today's quote.</span>
          </label>
        </div>
      {/if}

      {#if shape === 'trade' || shape === 'income'}
        <div class="row">
          {#if shape === 'trade'}
            <label class="field">
              <span class="label">Fee <span class="faint">(optional)</span></span>
              <input type="text" inputmode="decimal" bind:value={fee} placeholder="0.00" />
              <span class="hint faint">Capitalizes into cost basis.</span>
            </label>
          {/if}

          <label class="field">
            <span class="label">
              {shape === 'income' ? 'Withholding tax' : 'Tax'} <span class="faint">(optional)</span>
            </span>
            <input type="text" inputmode="decimal" bind:value={tax} placeholder="0.00" />
            <span class="hint faint">
              {shape === 'income'
                ? 'Deducted from the gross amount above; the account receives the net.'
                : 'Capitalizes into cost basis.'}
            </span>
          </label>
        </div>
      {/if}

      <!-- The vocabulary trap, stated rather than papered over. `interest` credits cash and
           means interest *received*; interest charged on a loan is a `fee`, which is the
           server's own vocabulary. Renaming either in the UI would put a second name on a
           concept that already has one, so the form explains instead. -->
      {#if portfolio.kind === 'liability' && type === 'interest'}
        <p class="note faint">
          This records interest <em>received</em>, which credits the account. Record interest
          charged on this loan as a <strong>Fee</strong>.
        </p>
      {/if}

      <!-- Not in the spec's field table, kept because the ledger already has a Memo column
           reading `event.memo`. Without a way to write one it could only ever show a
           `source_reference`, which is a different fact wearing the same column. -->
      <label class="field wide">
        <span class="label">Memo <span class="faint">(optional)</span></span>
        <input type="text" bind:value={memo} maxlength="200" />
      </label>

      <!-- Collapsed by default. Both fields exist for reconciling against a statement, which
           ordinary entry never needs, and a wrong settlement figure unbalances the event. -->
      <details bind:open={advancedOpen} class="advanced">
        <summary>Advanced</summary>

        <div class="row">
          <label class="field">
            <span class="label">Settlement amount ({portfolio.base_currency})</span>
            <input
              type="text"
              inputmode="decimal"
              bind:value={settlementAmount}
              placeholder="computed"
              aria-invalid={!!fieldError('settlement_amount')}
            />
            <span class="hint faint">
              The exact cash a broker reports, signed. Overrides the figure computed above; the
              event must still balance.
            </span>
            {#if fieldError('settlement_amount')}
              <span class="negative msg">{fieldError('settlement_amount')}</span>
            {/if}
          </label>

          <label class="field">
            <span class="label">Source reference</span>
            <input type="text" bind:value={sourceReference} maxlength="120" placeholder="Confirmation or statement ID" />
            <span class="hint faint">
              What makes this event reconcilable against the document it came from.
            </span>
          </label>
        </div>
      </details>

      <!-- One computed line, deliberately not a leg table. It catches a misplaced decimal
           before it reaches a ledger whose only correction is a reversal dated today. -->
      {#if effect !== null}
        <p class="effect">
          <span class="label">Cash effect</span>
          <span class="num" class:negative={effect < 0}>
            {exactMoney(String(effect), portfolio.base_currency)}
          </span>
        </p>
      {/if}

      {#if error && error.field === null}
        <p class="negative msg">{error.message}</p>
      {/if}

      <div class="actions">
        <button type="submit" class="primary" disabled={!canSubmit}>
          {submitting ? 'Posting…' : 'Post transaction'}
        </button>
        {#if postedOn && !error}
          <span class="positive">Posted.</span>
        {/if}
      </div>

      {#if staleDate}
        <StaleSnapshots from={staleDate} portfolioId={portfolio.id}>
          Snapshots from {shortDate(staleDate)} are now out of date, so performance does not yet
          reflect this entry.
        </StaleSnapshots>
      {/if}
    </form>
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
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h2 {
    font-size: 15px;
  }

  form {
    display: flex;
    flex-direction: column;
    gap: 14px;
    margin-top: 16px;
  }

  /* Two columns where there is room, one below ~640px. The fields are short, and a single
     column on a phone beats a squeezed pair. */
  .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .wide {
    max-width: 460px;
  }

  .label {
    font-size: 12px;
    color: var(--text-muted);
  }

  .hint {
    font-size: 11px;
  }

  .msg {
    font-size: 12px;
  }

  input,
  select {
    padding: 7px 10px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    min-width: 0;
  }

  input:focus-visible,
  select:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  input[aria-invalid='true'] {
    border-color: var(--negative);
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

  .toggle {
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
  }

  .advanced summary {
    font-size: 12px;
    color: var(--text-muted);
    cursor: pointer;
  }

  .advanced summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: var(--radius-sm);
  }

  .advanced .row {
    margin-top: 12px;
  }

  .note {
    max-width: 460px;
    margin: 0;
    font-size: 12px;
  }

  /* Advisory, not a refusal. `--warning` rather than `--negative`: the entry can still post,
     and colouring it as an error would teach the reader to ignore the real ones. */
  .warn {
    color: var(--warning);
  }

  /* The one computed figure on the form, so it reads as a statement rather than another box. */
  .effect {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    max-width: 460px;
    margin: 0;
    padding: 8px 10px;
    font-size: 14px;
    background: var(--surface-sunken);
    border-radius: var(--radius-sm);
  }
</style>
