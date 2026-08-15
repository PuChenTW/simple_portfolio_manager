<script lang="ts">
  import type { JournalEvent, JournalEventPage } from '../../api/client'
  import { PAGE_SIZE } from '../../account.svelte'
  import { money, quantity, shortDate } from '../../format'

  let {
    page,
    offset,
    currency,
    onpage,
  }: {
    page: JournalEventPage
    offset: number
    currency: string
    onpage: (offset: number) => void
  } = $props()

  /** Legs arrive inline, so expanding a row costs no request. */
  let expanded = $state<Record<string, boolean>>({})

  function toggle(id: string): void {
    expanded[id] = !expanded[id]
  }

  const from = $derived(page.total === 0 ? 0 : offset + 1)
  const to = $derived(Math.min(offset + page.items.length, page.total))
  const hasPrev = $derived(offset > 0)
  const hasNext = $derived(offset + page.items.length < page.total)

  // A reversal and a reversed event are both still posted; neither is deleted. Showing the
  // relationship is the whole point of an audit trail -- a row that reads as an ordinary trade
  // while having been undone is the one thing this table must not do.
  const reversedIds = $derived(
    new Set(page.items.map((e) => e.reverses_event_id).filter((id): id is string => !!id)),
  )

  function label(event: JournalEvent): string {
    return event.event_type.replace(/_/g, ' ')
  }
</script>

<section class="card">
  <header>
    <h2>Transactions</h2>
    <span class="faint">
      {#if page.total}
        {from}–{to} of {page.total}
      {:else}
        none recorded
      {/if}
    </span>
  </header>

  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th scope="col" class="chevron-col"><span class="visually-hidden">Expand</span></th>
          <th scope="col">Date</th>
          <th scope="col">Type</th>
          <th scope="col">Instrument</th>
          <th scope="col">Memo</th>
          <th scope="col" class="right">Cash</th>
        </tr>
      </thead>
      <tbody>
        {#each page.items as event (event.id)}
          {@const legs = event.legs ?? []}
          {@const cashLeg = legs.find((leg) => leg.leg_type === 'cash')}
          {@const tickers = [...new Set(legs.map((l) => l.ticker).filter(Boolean))]}
          {@const undone = reversedIds.has(event.id)}
          <tr class="event" class:voided={undone || !!event.reverses_event_id}>
            <td class="chevron-col">
              <button
                type="button"
                class="chevron"
                aria-expanded={!!expanded[event.id]}
                onclick={() => toggle(event.id)}
              >
                <span class="visually-hidden">
                  {expanded[event.id] ? 'Hide' : 'Show'} legs for {label(event)}
                </span>
                <span aria-hidden="true" class:open={expanded[event.id]}>›</span>
              </button>
            </td>
            <th scope="row" class="date num">{shortDate(event.trade_date ?? event.occurred_at)}</th>
            <td>
              <span class="type">{label(event)}</span>
              {#if event.reverses_event_id}
                <span class="tag" title="This event undoes an earlier one">reversal</span>
              {:else if undone}
                <span class="tag" title="A later reversal undid this event">reversed</span>
              {/if}
            </td>
            <td class="muted">{tickers.length ? tickers.join(', ') : '—'}</td>
            <td class="memo muted">{event.memo ?? event.source_reference ?? '—'}</td>
            <td class="right num" class:negative={Number(cashLeg?.amount_delta ?? 0) < 0}>
              {cashLeg?.amount_delta == null
                ? '—'
                : money(cashLeg.amount_delta, cashLeg.currency)}
            </td>
          </tr>

          {#if expanded[event.id]}
            <tr class="legs">
              <td colspan="6">
                <!-- Every leg, unnetted. The balance is the point: a reader checking a posting
                     needs to see the sides sum, not a summary that already assumed they do. -->
                <table class="inner">
                  <thead>
                    <tr>
                      <th scope="col">Leg</th>
                      <th scope="col">Role</th>
                      <th scope="col">Instrument</th>
                      <th scope="col" class="right">Quantity</th>
                      <th scope="col" class="right">Unit price</th>
                      <th scope="col" class="right">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each legs as leg, i (i)}
                      <tr>
                        <td>{leg.leg_type.replace(/_/g, ' ')}</td>
                        <td class="muted">{leg.account_role.replace(/_/g, ' ')}</td>
                        <td class="muted">{leg.ticker ?? '—'}</td>
                        <td class="right num">{quantity(leg.quantity_delta)}</td>
                        <td class="right num">
                          {leg.unit_price == null ? '—' : money(leg.unit_price, leg.currency)}
                        </td>
                        <td class="right num" class:negative={Number(leg.amount_delta ?? 0) < 0}>
                          {leg.amount_delta == null
                            ? '—'
                            : money(leg.amount_delta, leg.currency)}
                        </td>
                      </tr>
                    {:else}
                      <tr><td colspan="6" class="muted">This event has no legs.</td></tr>
                    {/each}
                  </tbody>
                </table>
                <p class="provenance faint">
                  {event.source}{event.source_reference ? ` · ${event.source_reference}` : ''} ·
                  request {event.request_id} · {event.flow_classification}
                </p>
              </td>
            </tr>
          {/if}
        {:else}
          <tr>
            <td colspan="6" class="empty muted">
              No transactions posted to this account yet.
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  {#if page.total > PAGE_SIZE}
    <nav class="pager">
      <button type="button" disabled={!hasPrev} onclick={() => onpage(Math.max(0, offset - PAGE_SIZE))}>
        Previous
      </button>
      <button type="button" disabled={!hasNext} onclick={() => onpage(offset + PAGE_SIZE)}>
        Next
      </button>
    </nav>
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
    margin-bottom: 12px;
  }

  h2 {
    font-size: 15px;
  }

  .scroll {
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    white-space: nowrap;
  }

  th,
  td {
    padding: 8px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }

  thead th {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-faint);
    font-weight: 600;
  }

  .right {
    text-align: right;
  }

  .type {
    text-transform: capitalize;
  }

  .date {
    font-weight: 500;
  }

  .memo {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Struck through rather than hidden: a reversed event stays in the ledger, and the reversal
     that undid it is a posting in its own right. */
  .voided .type {
    text-decoration: line-through;
    text-decoration-color: var(--text-faint);
  }

  .tag {
    margin-left: 6px;
    padding: 1px 7px;
    font-size: 10px;
    color: var(--text-faint);
    border: 1px dashed var(--border-strong);
    border-radius: 999px;
  }

  .chevron-col {
    width: 28px;
  }

  .chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    padding: 0;
    font: inherit;
    color: var(--text-faint);
    background: none;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .chevron:hover {
    background: var(--surface-sunken);
    color: var(--text);
  }

  .chevron span {
    display: block;
    transition: transform 120ms ease-out;
  }

  .chevron span.open {
    transform: rotate(90deg);
  }

  .legs > td {
    padding: 0 8px 12px 36px;
    background: var(--surface-sunken);
  }

  .inner {
    font-size: 12px;
  }

  .inner th,
  .inner td {
    padding: 5px 8px;
    border-bottom: 1px solid var(--border);
  }

  .inner tbody tr:last-child td {
    border-bottom: none;
  }

  .provenance {
    margin: 8px 0 0;
    font-size: 11px;
    white-space: normal;
    word-break: break-all;
  }

  .pager {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 12px;
  }

  .pager button {
    padding: 6px 12px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .pager button:disabled {
    color: var(--text-faint);
    cursor: default;
    opacity: 0.6;
  }

  .empty {
    text-align: center;
    padding: 20px;
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }

  @media (prefers-reduced-motion: reduce) {
    .chevron span {
      transition: none;
    }
  }
</style>
