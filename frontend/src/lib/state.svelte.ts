import { api, type ConsolidatedSummary, type Group, type Portfolio } from './api/client'

const LAST_GROUP_KEY = 'portfolio.lastGroupId'

function readLastGroupId(): string | null {
  try {
    return localStorage.getItem(LAST_GROUP_KEY)
  } catch {
    return null // Private browsing or a blocked origin; falling back to the first group is fine.
  }
}

function rememberGroupId(id: string): void {
  try {
    localStorage.setItem(LAST_GROUP_KEY, id)
  } catch {
    /* Remembering is a convenience, never a requirement. */
  }
}

export class DashboardState {
  groups = $state<Group[]>([])
  portfolios = $state<Portfolio[]>([])
  summary = $state<ConsolidatedSummary | null>(null)
  selectedGroupId = $state<string | null>(null)
  loading = $state(true)
  error = $state<string | null>(null)
  /** Milliseconds the in-flight request has been running; 0 when idle. */
  elapsed = $state(0)
  /** True while re-fetching with a summary already on screen (a group switch). */
  refreshing = $state(false)

  #timer: ReturnType<typeof setInterval> | null = null
  /** Guards against a slow response for a group the user already switched away from. */
  #requestSeq = 0

  get selectedGroup(): Group | null {
    return this.groups.find((g) => g.id === this.selectedGroupId) ?? null
  }

  #startClock(): void {
    this.#stopClock()
    const startedAt = Date.now()
    this.elapsed = 0
    // A coarse tick is enough: the only threshold is "slow enough to explain itself".
    this.#timer = setInterval(() => {
      this.elapsed = Date.now() - startedAt
    }, 250)
  }

  #stopClock(): void {
    if (this.#timer !== null) {
      clearInterval(this.#timer)
      this.#timer = null
    }
    this.elapsed = 0
  }

  async load(): Promise<void> {
    this.loading = true
    this.error = null
    this.#startClock()
    try {
      const [groups, portfolios] = await Promise.all([api.listGroups(), api.listPortfolios()])
      this.groups = groups
      this.portfolios = portfolios

      // Last viewed, else the first group. A remembered id that no longer exists must not
      // strand the page on an empty view.
      const remembered = readLastGroupId()
      const initial = groups.find((g) => g.id === remembered) ?? groups[0]
      if (initial) {
        await this.selectGroup(initial.id)
      }
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err)
    } finally {
      this.loading = false
      this.#stopClock()
    }
  }

  /** Re-read the current group after a mutation elsewhere in the app changed what it reports. */
  async refresh(): Promise<void> {
    if (this.selectedGroupId) await this.selectGroup(this.selectedGroupId)
  }

  /** Re-read the portfolio list too, for a mutation that renamed or removed an account.
   *
   * `refresh` re-reads only the summary, which would leave the Accounts card showing a name that
   * no longer exists -- or a deleted account still listed. */
  async reload(): Promise<void> {
    try {
      this.portfolios = await api.listPortfolios()
    } catch {
      /* The list is stale, not wrong; the summary below is the number that matters. */
    }
    await this.refresh()
  }

  async selectGroup(groupId: string): Promise<void> {
    this.selectedGroupId = groupId
    rememberGroupId(groupId)
    this.error = null

    // Switching groups keeps the old summary visible and dims it, rather than dropping back to
    // a skeleton: replacing a full page with placeholders reads as data loss, not as progress.
    const isSwitch = this.summary !== null
    this.refreshing = isSwitch
    if (!isSwitch) this.#startClock()

    const seq = ++this.#requestSeq
    try {
      const summary = await api.groupSummary(groupId)
      if (seq !== this.#requestSeq) return // A newer switch already won; discard this response.
      this.summary = summary
    } catch (err) {
      if (seq !== this.#requestSeq) return
      this.summary = null
      this.error = err instanceof Error ? err.message : String(err)
    } finally {
      if (seq === this.#requestSeq) {
        this.refreshing = false
        if (!isSwitch) this.#stopClock()
      }
    }
  }
}

export const dashboard = new DashboardState()
