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

// The API lives one level above this app: the page is served at `<root>/v2/`, the API at
// `<root>/api/v1/`. Resolving against `../` keeps every request relative to wherever the app is
// mounted, so a prefix-stripping proxy and a direct load both reach the same API. An absolute
// `/api/v1/` would work only at the domain root -- the exact bug docs/ARCHITECTURE.md records.
//
// In dev, Vite serves the app at `/` and proxies `/api`, so `../api/v1/` would escape the root.
// import.meta.env.DEV picks the right base at build time.
const API_ROOT = import.meta.env.DEV ? 'api/v1/' : '../api/v1/'

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
}
