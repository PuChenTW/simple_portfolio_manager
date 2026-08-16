<script lang="ts">
  import { api, type Portfolio, type TransactionCreate } from '../../api/client'
  import {
    acceptsTicker,
    cashEffect,
    errorFor,
    shapeFor,
    typesForKind,
    type ErrorField,
    type TransactionType,
  } from '../../transactions'
  import { exactMoney, shortDate } from '../../format'

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
  const shape = $derived(shapeFor(type))

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

  /** Whether this shape and this account together allow a ticker.
   *
   * Both halves matter. `reject_security_activity` refuses a ticker on a positionless book even
   * for a fee or interest event, because a ticker there attaches an instrument to the leg. A
   * field the server will refuse is a field this form must not render.
   */
  const showTicker = $derived(
    acceptsTicker(type) && portfolio.kind !== 'cash' && portfolio.kind !== 'liability',
  )

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

  /** The settlement cash this entry will move, recomputed as the user types.
   *
   * A single line, not a leg table: a full preview would duplicate more of `build_legs` in
   * TypeScript, and the copy would drift from the server that actually posts.
   */
  // Only the fields this shape actually renders are passed. A fee typed into a previous trade
  // still sits in `fee` when the type switches to a deposit, and the preview must not quietly
  // charge it against an entry whose form never showed it.
  const effect = $derived(
    shape === 'trade'
      ? cashEffect({ type, quantity, unitPrice, fee, tax })
      : shape === 'income'
        ? cashEffect({ type, amount, tax })
        : cashEffect({ type, amount }),
  )

  /** `YYYY-MM-DD` in local time. `toISOString` shifts the date across a timezone boundary. */
  function today(): string {
    const now = new Date()
    const month = String(now.getMonth() + 1).padStart(2, '0')
    const day = String(now.getDate()).padStart(2, '0')
    return `${now.getFullYear()}-${month}-${day}`
  }

  // Bound to `occurred_at`, never `trade_date`. `replay.py` filters and orders on `occurred_at`
  // alone -- `trade_date` is display metadata that drives no valuation. A date collected into
  // `trade_date` would read as June in the table below and replay as today, which breaks
  // invariant 5 with nothing on screen to show for it.
  //
  // It persists between entries rather than resetting: statement entry is a burst of
  // transactions sharing one date, and re-typing it each time is where a wrong date comes from.
  let occurredOn = $state(today())

  /** One idempotency key per form session, re-minted only after a success.
   *
   * A double-clicked submit sends the same id twice. The server fingerprints the payload against
   * it, matches, and returns the event it already wrote -- one posting, not two. A *failed*
   * submit keeps the key, so the retry is the same logical mutation rather than a second one.
   */
  let requestId = $state(crypto.randomUUID())

  let submitting = $state(false)
  let error = $state<{ field: ErrorField; message: string } | null>(null)
  /** The date of the last successful posting, or null once nothing needs saying about it. */
  let postedOn = $state<string | null>(null)

  function fieldError(field: ErrorField): string | null {
    return error && error.field === field ? error.message : null
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
    resolved = null
    unresolved = null
    error = null
    requestId = crypto.randomUUID()
    if (full) {
      type = types.includes('deposit') ? 'deposit' : types[0]
      occurredOn = today()
      postedOn = null
    }
  }

  // Enough to post, not everything the server checks. A required box being empty is the client's
  // to catch; whether the account holds the cash or the shares is the server's. See
  // "No client-side blocking" in the spec -- every refusal comes from the ledger.
  const complete = $derived(
    shape === 'trade'
      ? !!ticker.trim() && Number(quantity) > 0 && Number(unitPrice) > 0
      : Number(amount) > 0,
  )
  const canSubmit = $derived(!submitting && !!occurredOn && complete)

  /** A back-dated posting invalidates every snapshot from its date forward.
   *
   * Saying so is the whole point: without it the user gets a correct ledger and a performance
   * chart that still reports the old numbers, with nothing on screen connecting the two. It does
   * not rebuild automatically -- a force rebuild replaces stored valuations, which is why
   * `AccountSnapshots` gates it behind a second click naming how many.
   */
  const staleFrom = $derived(postedOn && postedOn < today() ? postedOn : null)

  function payload(): TransactionCreate {
    const body: TransactionCreate = {
      request_id: requestId,
      transaction_type: type,
      // The date as typed, with midnight appended and *no* timezone conversion.
      //
      // `new Date(...).toISOString()` is the trap here, and it is silent. It reads the string as
      // local midnight and re-serializes it in UTC, so east of Greenwich a date entered as the
      // 16th is stored as the 15th. `replay.py` filters on `occurred_at`, so the event then
      // replays into the wrong day while the form that sent it still displays the right one.
      // Sending the naive local datetime keeps the calendar date the user typed.
      occurred_at: `${occurredOn}T00:00:00`,
    }
    if (shape === 'trade') {
      body.ticker = ticker.trim().toUpperCase()
      body.quantity = quantity.trim()
      body.unit_price = unitPrice.trim()
    } else {
      body.amount = amount.trim()
      if (showTicker && ticker.trim()) body.ticker = ticker.trim().toUpperCase()
    }
    // Omitted rather than sent as "0": the server already defaults both, and an absent field is
    // the honest way to say the entry carried no fee. A cash movement has neither box, so its
    // stale values from a previous entry must never ride along.
    if (shape === 'trade' && fee.trim()) body.fee = fee.trim()
    if (shape !== 'cash' && tax.trim()) body.tax = tax.trim()
    if (memo.trim()) body.memo = memo.trim()
    return body
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault()
    if (!canSubmit) return

    submitting = true
    error = null
    try {
      await api.recordTransaction(portfolio.id, payload())
      postedOn = occurredOn
      // Type and date survive: the next line of a statement usually shares both.
      reset(false)
      await onposted()
      amountInput?.focus()
    } catch (err) {
      error = errorFor(err)
    } finally {
      submitting = false
    }
  }

  let amountInput: HTMLInputElement | null = $state(null)

  const TYPE_LABELS: Record<string, string> = {
    buy: 'Buy',
    sell: 'Sell',
    deposit: 'Deposit',
    withdrawal: 'Withdrawal',
    transfer_in: 'Transfer in',
    transfer_out: 'Transfer out',
    dividend: 'Dividend',
    interest: 'Interest received',
    fee: 'Fee',
    tax: 'Tax',
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
        <select bind:value={type} onchange={() => (error = null)}>
          {#each types as option (option)}
            <option value={option}>{TYPE_LABELS[option] ?? option}</option>
          {/each}
        </select>
      </label>

      <div class="row">
        <label class="field">
          <span class="label">Date</span>
          <input type="date" bind:value={occurredOn} required />
          <span class="hint faint">The date the transaction happened, not today.</span>
        </label>

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

      <label class="field wide">
        <span class="label">Memo <span class="faint">(optional)</span></span>
        <input type="text" bind:value={memo} maxlength="200" />
      </label>

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

      {#if staleFrom}
        <p class="stale">
          Snapshots from {shortDate(staleFrom)} are now out of date. Rebuild them from the
          <a href="#/account/{portfolio.id}/settings">Snapshots panel</a> in Settings so
          performance reflects this entry.
        </p>
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

  .stale {
    margin: 0;
    padding: 8px 10px;
    font-size: 12px;
    background: var(--surface-sunken);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
</style>
