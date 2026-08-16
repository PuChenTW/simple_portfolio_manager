<script lang="ts">
  import { api, type Portfolio, type TransactionCreate } from '../../api/client'
  import {
    errorFor,
    shapeFor,
    typesForKind,
    type ErrorField,
    type TransactionType,
  } from '../../transactions'
  import { shortDate } from '../../format'

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
    error = null
    requestId = crypto.randomUUID()
    if (full) {
      type = types.includes('deposit') ? 'deposit' : types[0]
      occurredOn = today()
      postedOn = null
    }
  }

  const amountValid = $derived(Number(amount) > 0)
  const canSubmit = $derived(!submitting && !!occurredOn && amountValid)

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
    if (shape === 'cash') body.amount = amount.trim()
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

        {#if shape === 'cash'}
          <label class="field">
            <span class="label">Amount ({portfolio.base_currency})</span>
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

      <label class="field wide">
        <span class="label">Memo <span class="faint">(optional)</span></span>
        <input type="text" bind:value={memo} maxlength="200" />
      </label>

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

  .stale {
    margin: 0;
    padding: 8px 10px;
    font-size: 12px;
    background: var(--surface-sunken);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
</style>
