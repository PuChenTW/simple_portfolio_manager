<script lang="ts">
  import { account } from '../../account.svelte'
  import { router, type AccountTab } from '../../route.svelte'
  import type { Portfolio } from '../../api/client'
  import { shortDate } from '../../format'
  import AccountHoldings from './AccountHoldings.svelte'
  import AccountTransactions from './AccountTransactions.svelte'
  import RecordTransaction from './RecordTransaction.svelte'
  import AccountPerformance from './AccountPerformance.svelte'
  import AccountSettings from './AccountSettings.svelte'
  import ThemeToggle from '../ThemeToggle.svelte'

  let {
    portfolioId,
    tab,
    groupName,
    onchange,
  }: {
    portfolioId: string
    tab: AccountTab
    /** The group to return to, so the breadcrumb names where "back" goes. */
    groupName: string | null
    /** Something changed that the dashboard's own lists also report. */
    onchange: () => void
  } = $props()

  // Tabs of one account, in the order they are usually needed: what is held, how it got there,
  // how it did, and only then how it is labelled.
  const ALL_TABS: { id: AccountTab; label: string }[] = [
    { id: 'holdings', label: 'Holdings' },
    { id: 'transactions', label: 'Transactions' },
    { id: 'performance', label: 'Performance' },
    { id: 'settings', label: 'Settings' },
  ]

  $effect(() => {
    account.open(portfolioId)
  })

  const portfolio = $derived(account.portfolio)

  // A cash account earns no rate of return worth reporting: its balance moves by deposits and
  // withdrawals, which TWR neutralizes as external flows. The tab held one line of text saying
  // so, which is a tab that can never say anything else.
  const tabs = $derived(
    portfolio?.kind === 'cash' ? ALL_TABS.filter((t) => t.id !== 'performance') : ALL_TABS,
  )

  // A bookmarked `#/account/<id>/performance` outlives the tab. Fall back to the first tab
  // rather than rendering an empty panel under a tablist with nothing selected.
  const current = $derived(tabs.some((t) => t.id === tab) ? tab : 'holdings')

  // A tab fetches when it is first shown, not when the page loads. Valuing holdings costs a
  // quote per ticker and performance reads a year of snapshots -- paying for all four on arrival
  // would make every visit as slow as the slowest tab.
  $effect(() => {
    if (current === 'holdings') account.ensureSummary()
    else if (current === 'transactions') account.ensureTransactions()
    else if (current === 'performance') account.ensurePerformance()
  })

  // A cash or liability account holds no securities, so its "holdings" tab is a balance. The
  // label follows the kind rather than pretending every book is a brokerage account.
  const holdingsLabel = $derived(
    portfolio && portfolio.kind !== 'investment' ? 'Balance' : 'Holdings',
  )

  const KIND_LABEL: Record<string, string> = {
    investment: 'Investment account',
    cash: 'Cash account',
    liability: 'Liability',
  }

  /** Arrow keys move between tabs, which is what a tablist is expected to do. Focus follows the
   *  selection, otherwise the next arrow press would move from the tab that was left behind. */
  function onKeydown(event: KeyboardEvent): void {
    const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
    if (!delta) return
    event.preventDefault()

    const index = tabs.findIndex((t) => t.id === current)
    const next = tabs[(index + delta + tabs.length) % tabs.length]
    location.hash = router.account(portfolioId, next.id)

    const list = (event.currentTarget as HTMLElement).parentElement
    // The roving tabindex only updates once the new route has rendered.
    queueMicrotask(() => list?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus())
  }

  function onSaved(updated: Portfolio): void {
    account.applyIdentity(updated)
    onchange() // The account lists on the dashboard show the old name until they re-read.
  }

  function onDeleted(): void {
    onchange()
    location.hash = router.dashboard()
  }

  /** The date a reversal invalidated snapshots from, or null when nothing needs saying.
   *
   * A reversal is dated today, but it undoes an event that may not have been. Replay reads
   * `occurred_at`, so every snapshot from the *original's* date forward is now wrong -- the same
   * situation a back-dated posting creates, and it needs the same notice. */
  let reversalStaleFrom = $state<string | null>(null)

  async function onReversed(originalOccurredAt: string): Promise<void> {
    const day = originalOccurredAt.slice(0, 10)
    const today = new Date()
    const iso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
    reversalStaleFrom = day < iso ? day : null
    await account.afterPosting()
  }
</script>

<div class="page">
  <!-- The account page renders instead of the group header, not below it, so the toggle has to
       appear here as well or it would be unreachable from any account. -->
  <div class="crumb">
    <a href={router.dashboard()}>← {groupName ?? 'Dashboard'}</a>
    <ThemeToggle />
  </div>

  {#if account.identity.loading}
    <div class="placeholder" aria-busy="true">Loading account…</div>
  {:else if account.identity.error}
    <p class="state error">{account.identity.error}</p>
  {:else if portfolio}
    <header class="head">
      <div>
        <h1>{portfolio.name}</h1>
        <p class="faint sub">
          {KIND_LABEL[portfolio.kind] ?? portfolio.kind} · {portfolio.base_currency}{portfolio.institution
            ? ` · ${portfolio.institution}`
            : ''}
        </p>
      </div>
    </header>

    <!-- Anchors, not buttons: a tab is a URL here, so it opens in a new tab and survives a
         reload. The roving tabindex and arrow keys live on the tabs themselves, since they are
         what receives focus. -->
    <div class="tabs" role="tablist" aria-label="Account sections">
      {#each tabs as item (item.id)}
        <a
          role="tab"
          href={router.account(portfolioId, item.id)}
          class:current={current === item.id}
          aria-selected={current === item.id}
          tabindex={current === item.id ? 0 : -1}
          onkeydown={onKeydown}
        >
          {item.id === 'holdings' ? holdingsLabel : item.label}
        </a>
      {/each}
    </div>

    <div class="panel" role="tabpanel">
      {#if current === 'holdings'}
        {#if account.summary.loading}
          <div class="placeholder" aria-busy="true">Valuing holdings…</div>
        {:else if account.summary.error}
          <p class="state error">{account.summary.error}</p>
        {:else if account.summary.data}
          <AccountHoldings summary={account.summary.data} />
        {/if}
      {:else if current === 'transactions'}
        <!-- The form sits above the ledger rather than in a modal: the table below is the
             confirmation that a posting landed, so the two share one viewport. It renders
             independently of the ledger's load state, since a failed page read is no reason to
             refuse a posting. -->
        <div class="stack">
          <RecordTransaction {portfolio} onposted={() => account.afterPosting()} />

          {#if reversalStaleFrom}
            <p class="stale">
              That reversal undid an event dated {shortDate(reversalStaleFrom)}, so snapshots from
              that date are now out of date. Rebuild them from the
              <a href={router.account(portfolioId, 'settings')}>Snapshots panel</a> in Settings.
            </p>
          {/if}

          {#if account.transactions.loading}
            <div class="placeholder" aria-busy="true">Loading transactions…</div>
          {:else if account.transactions.error}
            <p class="state error">{account.transactions.error}</p>
          {:else if account.transactions.data}
            <AccountTransactions
              page={account.transactions.data}
              offset={account.offset}
              portfolioId={portfolio.id}
              currency={portfolio.base_currency}
              onpage={(offset) => account.loadTransactions(offset)}
              onreversed={onReversed}
            />
          {/if}
        </div>
      {:else if current === 'performance'}
        {#if account.performance.loading}
          <div class="placeholder" aria-busy="true">Reading snapshots…</div>
        {:else if account.performance.error}
          <!-- A liability book returns no rate of return by design, and a portfolio with no
               snapshots at either end cannot be measured. Both arrive here as an error with a
               reason attached, which is the answer rather than a failure to hide. -->
          <p class="state error">{account.performance.error}</p>
        {:else if account.performance.data}
          <AccountPerformance
            performance={account.performance.data}
            navHistory={account.navHistory.data}
            range={account.range}
            activePreset={account.preset}
            onrange={(start, end) => account.setRange(start, end)}
            onpreset={(id) => account.setPreset(id, portfolio.first_event_date ?? null)}
          />
        {/if}
      {:else if current === 'settings'}
        <AccountSettings {portfolio} onsaved={onSaved} ondeleted={onDeleted} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .crumb {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .crumb a {
    font-size: 13px;
    color: var(--text-muted);
    text-decoration: none;
  }

  .crumb a:hover {
    color: var(--text);
  }

  .head h1 {
    font-size: 20px;
  }

  .sub {
    margin: 2px 0 0;
    font-size: 12px;
  }

  /* An underlined tablist rather than pills: these are sections of one account, not the
     top-level destinations the header nav already renders as pills. */
  .tabs {
    display: flex;
    gap: 2px;
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
    /* The tabs overhang the bottom by 1px so their underline covers the border above. That is a
       vertical overflow, and `overflow-x: auto` coerces the other axis from `visible` to `auto`
       -- which renders a 1px vertical scrollbar wherever scrollbars are always shown. Only the
       horizontal axis is meant to scroll here. */
    overflow-y: hidden;
  }

  .tabs a {
    padding: 8px 14px;
    font-size: 13px;
    color: var(--text-muted);
    text-decoration: none;
    white-space: nowrap;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
  }

  .tabs a:hover {
    color: var(--text);
  }

  .tabs a.current {
    color: var(--text);
    font-weight: 600;
    border-bottom-color: var(--accent);
  }

  .tabs a:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
    border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  }

  .stale {
    margin: 0;
    padding: 8px 10px;
    font-size: 12px;
    background: var(--surface-sunken);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .stack {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .placeholder {
    padding: 40px 0;
    text-align: center;
    color: var(--text-faint);
    font-size: 13px;
  }

  .state {
    padding: 40px 0;
    text-align: center;
  }

  .error {
    color: var(--negative);
  }
</style>
