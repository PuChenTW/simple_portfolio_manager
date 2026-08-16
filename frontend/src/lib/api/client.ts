import type { components } from './schema'

type Schemas = components['schemas']

export type ConsolidatedSummary = Schemas['ConsolidatedSummaryRead']
export type ConsolidatedPosition = Schemas['ConsolidatedPositionRead']
export type CurrencyTotal = Schemas['CurrencyTotalRead']
export type IssuerExposure = Schemas['IssuerExposureRead']
export type UnconvertedAmount = Schemas['UnconvertedAmountRead']
export type Group = Schemas['GroupRead']
export type Portfolio = Schemas['PortfolioRead']
export type PortfolioKind = Schemas['PortfolioKind']
export type AssetClass = Schemas['AssetClass']
export type Provenance = Schemas['Provenance']
export type InstrumentProfile = Schemas['InstrumentProfileRead']
export type PortfolioSummary = Schemas['PortfolioSummary']
export type Position = Schemas['PositionRead']
export type JournalEventPage = Schemas['JournalEventPage']
export type JournalEvent = Schemas['JournalEventRead']
export type JournalLeg = Schemas['JournalLegRead']
export type Performance = Schemas['PerformanceRead']
export type NavHistory = Schemas['NavHistoryRead']
export type SnapshotSummary = Schemas['SnapshotSummary']
export type Rebuild = Schemas['RebuildRead']

/** Every asset class the API defines, in taxonomy order.
 *
 * Listed rather than derived because a TypeScript union is erased at runtime. The compiler still
 * checks it: `AssetClass[]` fails to build if a member is misspelled, and the exhaustiveness
 * assertion below fails if the API adds one this list has not caught up with -- so a new taxonomy
 * member breaks the build rather than silently going missing from the picker.
 */
export const ASSET_CLASSES = [
  'equity',
  'fixed_income',
  'cash',
  'cash_equivalent',
  'commodity',
  'real_estate',
  'crypto',
  'multi_asset',
  'alternative',
  'unclassified',
] as const satisfies readonly AssetClass[]

// Fails to compile if the API gains an asset class missing from ASSET_CLASSES.
const _exhaustive: (typeof ASSET_CLASSES)[number] = null as unknown as AssetClass
void _exhaustive

/** A machine-readable API failure. The envelope is stable, so surface `code` rather than status. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

// Relative, with no leading slash and no `../`. The app is served at the same root as the API --
// the page at `<root>/`, the API at `<root>/api/v1/` -- so resolving against `document.baseURI`
// reaches the API wherever the app is mounted. A prefix-stripping proxy and a direct load both
// work. An absolute `/api/v1/` would work only at the domain root, which is the exact bug
// docs/ARCHITECTURE.md records.
//
// This was `../api/v1/` while the app was served one level down at `<root>/v2/`. Promoting it to
// the root made that one level too high: harmless at a domain root, where `../` cannot climb past
// `/`, and wrong under a proxy that mounts the app below one.
const API_ROOT = 'api/v1/'

function apiUrl(path: string): URL {
  return new URL(API_ROOT + path, document.baseURI)
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path), {
    headers: { accept: 'application/json' },
  })
  if (!response.ok) {
    throw await toApiError(response)
  }
  return response.json() as Promise<T>
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: 'PUT',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw await toApiError(response)
  }
  return response.json() as Promise<T>
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw await toApiError(response)
  }
  return response.json() as Promise<T>
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw await toApiError(response)
  }
  return response.json() as Promise<T>
}

/** DELETE returns 204 with no body, so there is nothing to parse on success. */
async function del(path: string): Promise<void> {
  const response = await fetch(apiUrl(path), { method: 'DELETE', headers: { accept: '*/*' } })
  if (!response.ok) {
    throw await toApiError(response)
  }
}

/** Query string from defined values only, so an omitted option never becomes `undefined`. */
function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

async function toApiError(response: Response): Promise<ApiError> {
  const fallback = `Request failed with status ${response.status}`
  try {
    const body = await response.json()
    const detail = body?.detail ?? body
    return new ApiError(response.status, detail?.code ?? 'unknown', detail?.message ?? fallback)
  } catch {
    return new ApiError(response.status, 'unknown', fallback)
  }
}

export const api = {
  listGroups: () => get<Group[]>('portfolio-groups'),
  groupSummary: (groupId: string) =>
    get<ConsolidatedSummary>(`portfolio-groups/${encodeURIComponent(groupId)}/summary`),
  listPortfolios: () => get<Portfolio[]>('portfolios'),

  // --- One account -----------------------------------------------------------------------
  //
  // The consolidated summary reports a group in one reporting currency. These read a single
  // portfolio in *its own* base currency, which is why an account page cannot be assembled by
  // filtering the group summary: the per-holding cost basis, P&L, and tags it shows are not on
  // ConsolidatedPositionRead at all, and its values would carry a converted currency.

  portfolio: (portfolioId: string) =>
    get<Portfolio>(`portfolios/${encodeURIComponent(portfolioId)}`),

  portfolioSummary: (portfolioId: string) =>
    get<PortfolioSummary>(`portfolios/${encodeURIComponent(portfolioId)}/summary`),

  journalEvents: (
    portfolioId: string,
    options: { offset?: number; limit?: number; eventType?: string } = {},
  ) =>
    get<JournalEventPage>(
      `portfolios/${encodeURIComponent(portfolioId)}/transactions` +
        query({
          offset: options.offset,
          limit: options.limit,
          event_type: options.eventType,
          // One request per page rather than one per row. See API version history 0.3.0.
          include_legs: true,
        }),
    ),

  performance: (portfolioId: string, startDate: string, endDate: string) =>
    get<Performance>(
      `portfolios/${encodeURIComponent(portfolioId)}/performance` +
        query({ start_date: startDate, end_date: endDate }),
    ),

  navHistory: (portfolioId: string, startDate: string, endDate: string) =>
    get<NavHistory>(
      `portfolios/${encodeURIComponent(portfolioId)}/nav-history` +
        query({ start_date: startDate, end_date: endDate }),
    ),

  /** Build snapshots across one date range.
   *
   * Callers pass a range no wider than a calendar month. The endpoint is synchronous -- it
   * replays the journal once per date inside the request -- so a multi-year range would hold
   * the connection open past any browser timeout, and the work would continue server-side with
   * nothing watching. Month alignment is not an arbitrary size: the Redis history cache buckets
   * bars by calendar month, so a month-aligned request asks for exactly what it stores.
   *
   * `force` replaces snapshots that already exist. Without it those dates are skipped, which is
   * what makes an interrupted run recoverable by simply repeating it.
   */
  rebuildSnapshots: (portfolioId: string, startDate: string, endDate: string, force = false) =>
    post<Rebuild>(`portfolios/${encodeURIComponent(portfolioId)}/valuation-snapshots/rebuild`, {
      start_date: startDate,
      end_date: endDate,
      force_revision: force,
    }),

  /** Rename an account or record who holds it. Currency and kind are fixed at creation. */
  updatePortfolio: (portfolioId: string, body: { name?: string; institution?: string }) =>
    patch<Portfolio>(`portfolios/${encodeURIComponent(portfolioId)}`, body),

  /** Cascade-deletes every position, event, and snapshot. There is no undo. */
  deletePortfolio: (portfolioId: string) =>
    del(`portfolios/${encodeURIComponent(portfolioId)}`),

  /** Record a manual asset-class decision, or retract one to restore the provider's view.
   *
   * `request_id` is an idempotency key for one logical mutation, so it is generated per call
   * rather than per instrument: two deliberate edits to the same ticker are two mutations, and
   * reusing a key would make the second a silent no-op replay of the first.
   */
  setAssetClass: (
    reference: string,
    body: { value?: AssetClass; reason: string; retract?: boolean },
  ) =>
    put<InstrumentProfile>(`instruments/${encodeURIComponent(reference)}/classification`, {
      request_id: crypto.randomUUID(),
      field: 'asset_class',
      ...body,
    }),
}
