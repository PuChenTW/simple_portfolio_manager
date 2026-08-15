/** Hash routing, deliberately.
 *
 * The fragment never reaches the server, so it is the one routing mechanism that survives this
 * app's deployment story unchanged. Tailscale Serve strips its path prefix before forwarding and
 * sends no `X-Forwarded-Prefix`, which makes a proxied request byte-identical to a direct one --
 * so path-based routes would need a base the app cannot discover. See docs/ARCHITECTURE.md,
 * "Sub-path deployment". A hash needs no base at all: `#/classify` means the same thing at
 * `localhost:8001/static/v2/` and at `example.com/portfolio/static/v2/`.
 */

/** Which page is on screen. An account page also carries the id it is showing. */
export type Route =
  | { name: 'dashboard' }
  | { name: 'classify' }
  | { name: 'account'; id: string; tab: AccountTab }

/** The tabs of one account page. `holdings` is the default when the hash names none. */
export type AccountTab = 'holdings' | 'transactions' | 'performance' | 'settings'

const ACCOUNT_TABS = ['holdings', 'transactions', 'performance', 'settings'] as const

function parseTab(value: string | undefined): AccountTab {
  return (ACCOUNT_TABS as readonly string[]).includes(value ?? '')
    ? (value as AccountTab)
    : 'holdings'
}

function parse(hash: string): Route {
  // `#/account/<id>/<tab>` -- the id is a UUID, so splitting on `/` is unambiguous.
  const segments = hash.replace(/^#\/?/, '').split('/').filter(Boolean)

  if (segments[0] === 'account' && segments[1]) {
    return { name: 'account', id: decodeURIComponent(segments[1]), tab: parseTab(segments[2]) }
  }
  if (segments[0] === 'classify') return { name: 'classify' }
  return { name: 'dashboard' }
}

class Router {
  route = $state<Route>(parse(location.hash))

  constructor() {
    addEventListener('hashchange', () => {
      this.route = parse(location.hash)
    })
  }

  /** The current page's name, for the `{#if}` that picks a view. */
  get name(): Route['name'] {
    return this.route.name
  }

  /** Hrefs are relative, so they compose with whatever path the app is mounted at. */
  dashboard(): string {
    return '#/'
  }

  classify(): string {
    return '#/classify'
  }

  /** A tab is part of the URL so a reload, a back button, and a shared link all land on it. */
  account(id: string, tab: AccountTab = 'holdings'): string {
    const base = `#/account/${encodeURIComponent(id)}`
    return tab === 'holdings' ? base : `${base}/${tab}`
  }
}

export const router = new Router()
