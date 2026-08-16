import {
  api,
  type JournalEventPage,
  type NavHistory,
  type Performance,
  type Portfolio,
  type PortfolioSummary,
} from './api/client'

/** One request's lifecycle. The three states are distinct: an error must not read as empty. */
export type Load<T> = {
  data: T | null
  loading: boolean
  error: string | null
}

function idle<T>(): Load<T> {
  return { data: null, loading: false, error: null }
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export const PAGE_SIZE = 25

/** Default performance window: the trailing year, which is the span most snapshots cover.
 *
 * This is deliberately the same range the `1y` preset produces, so the tab opens with that
 * preset highlighted rather than with no preset selected over a range one of them describes. */
function defaultRange(): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end)
  start.setFullYear(start.getFullYear() - 1)
  return { start: iso(start), end: iso(end) }
}

/** `YYYY-MM-DD` in local time. `toISOString` would shift the date across a timezone boundary. */
function iso(value: Date): string {
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${value.getFullYear()}-${month}-${day}`
}

export type RangePresetId = 'ytd' | '1y' | '3y' | '5y' | 'all'

export const RANGE_PRESETS: ReadonlyArray<{ id: RangePresetId; label: string }> = [
  { id: 'ytd', label: 'YTD' },
  { id: '1y', label: '1Y' },
  { id: '3y', label: '3Y' },
  { id: '5y', label: '5Y' },
  { id: 'all', label: 'All' },
]

/**
 * The dates a preset means, clamped to where the account's history actually starts.
 *
 * A preset names a whole range, so it pins `end` to today rather than keeping whatever was
 * there: "1 year" ending on a stale date states something false about what is displayed.
 *
 * The clamp is the reason `firstEventDate` is a parameter. A 5Y preset on an account with
 * fourteen months of history would otherwise ask the performance endpoint for a beginning value
 * from before the first event, which is a value that does not exist. `all` has no span of its
 * own and is entirely the clamp.
 */
export function presetRange(
  id: RangePresetId,
  firstEventDate: string | null,
): { start: string; end: string } {
  const today = new Date()
  const start = new Date(today)

  if (id === 'ytd') start.setMonth(0, 1)
  else if (id === '1y') start.setFullYear(start.getFullYear() - 1)
  else if (id === '3y') start.setFullYear(start.getFullYear() - 3)
  else if (id === '5y') start.setFullYear(start.getFullYear() - 5)

  const wanted = id === 'all' ? (firstEventDate ?? iso(start)) : iso(start)
  // String comparison is correct and cheap here: `YYYY-MM-DD` sorts lexicographically.
  const clamped = firstEventDate && wanted < firstEventDate ? firstEventDate : wanted
  return { start: clamped, end: iso(today) }
}


/**
 * One account's page.
 *
 * Each tab owns its request and fires on first visit, not on page load: valuing holdings fetches
 * a quote per ticker and performance reads a year of snapshots, so loading all four up front
 * would make every visit pay for the three tabs nobody opened. Loaded data is then kept, so
 * returning to a tab is instant.
 */
export class AccountState {
  portfolioId = $state<string | null>(null)

  identity = $state<Load<Portfolio>>(idle())
  summary = $state<Load<PortfolioSummary>>(idle())
  transactions = $state<Load<JournalEventPage>>(idle())
  performance = $state<Load<Performance>>(idle())
  navHistory = $state<Load<NavHistory>>(idle())

  offset = $state(0)
  range = $state(defaultRange())

  /** Which preset button produced `range`, or null once the dates are hand-edited.
   *
   * This lives here rather than in the performance component because loading a new range swaps
   * that component for a placeholder, destroying its state -- a click would clear its own
   * highlight. Seeded to `1y` because `defaultRange` is exactly what that preset produces. */
  preset = $state<RangePresetId | null>('1y')

  /** Discards a response for an account the user already navigated away from. */
  #seq = 0

  get portfolio(): Portfolio | null {
    return this.identity.data
  }

  /** Point the page at an account. A repeat call for the same id keeps what is already loaded. */
  open(portfolioId: string): void {
    if (this.portfolioId === portfolioId) return

    this.#seq += 1
    this.portfolioId = portfolioId
    this.identity = idle()
    this.summary = idle()
    this.transactions = idle()
    this.performance = idle()
    this.navHistory = idle()
    this.offset = 0
    this.range = defaultRange()
    this.preset = '1y'

    void this.#run((v) => (this.identity = v), () => api.portfolio(portfolioId))
  }

  /** Run a request into one slot, ignoring it if the page moved on while it was in flight.
   *
   * The slot is passed as a setter rather than a key: assigning through `this[key]` defeats the
   * generic, since TypeScript cannot prove the request's type matches the slot's. */
  async #run<T>(assign: (value: Load<T>) => void, request: () => Promise<T>): Promise<void> {
    const seq = this.#seq
    assign({ data: null, loading: true, error: null })
    try {
      const data = await request()
      if (seq !== this.#seq) return
      assign({ data, loading: false, error: null })
    } catch (err) {
      if (seq !== this.#seq) return
      assign({ data: null, loading: false, error: message(err) })
    }
  }

  /** Called when a tab becomes visible. Already-loaded data is not re-fetched. */
  ensureSummary(): void {
    const id = this.portfolioId
    if (!id || this.summary.data || this.summary.loading) return
    void this.#run((v) => (this.summary = v), () => api.portfolioSummary(id))
  }

  ensureTransactions(): void {
    const id = this.portfolioId
    if (!id || this.transactions.data || this.transactions.loading) return
    void this.loadTransactions(0)
  }

  ensurePerformance(): void {
    const id = this.portfolioId
    if (!id || this.performance.data || this.performance.loading) return
    void this.loadPerformance()
  }

  async loadTransactions(offset: number): Promise<void> {
    const id = this.portfolioId
    if (!id) return
    this.offset = offset
    await this.#run(
      (v) => (this.transactions = v),
      () => api.journalEvents(id, { offset, limit: PAGE_SIZE }),
    )
  }

  /** Performance and the NAV series are one view of the same period, so they load together. */
  async loadPerformance(): Promise<void> {
    const id = this.portfolioId
    if (!id) return
    const { start, end } = this.range
    await Promise.all([
      this.#run((v) => (this.performance = v), () => api.performance(id, start, end)),
      this.#run((v) => (this.navHistory = v), () => api.navHistory(id, start, end)),
    ])
  }

  /** A hand-picked range. It belongs to no preset, and highlighting one would claim the view is
   *  something it is not. */
  setRange(start: string, end: string): void {
    this.range = { start, end }
    this.preset = null
    void this.loadPerformance()
  }

  /** A preset range. The id is remembered rather than re-derived from the dates, because the
   *  clamp to `first_event_date` makes presets collide: on a three-year-old account, 5Y and All
   *  produce identical dates, and deriving the highlight would credit the wrong button. */
  setPreset(id: RangePresetId, firstEventDate: string | null): void {
    this.range = presetRange(id, firstEventDate)
    this.preset = id
    void this.loadPerformance()
  }

  /** After a rename, the identity header and the account lists elsewhere both need the new name. */
  applyIdentity(portfolio: Portfolio): void {
    this.identity = { data: portfolio, loading: false, error: null }
  }
}

export const account = new AccountState()
