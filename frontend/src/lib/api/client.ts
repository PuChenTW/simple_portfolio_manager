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
