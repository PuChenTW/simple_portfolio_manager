/** Hash routing, deliberately.
 *
 * The fragment never reaches the server, so it is the one routing mechanism that survives this
 * app's deployment story unchanged. Tailscale Serve strips its path prefix before forwarding and
 * sends no `X-Forwarded-Prefix`, which makes a proxied request byte-identical to a direct one --
 * so path-based routes would need a base the app cannot discover. See docs/ARCHITECTURE.md,
 * "Sub-path deployment". A hash needs no base at all: `#/classify` means the same thing at
 * `localhost:8001/static/v2/` and at `example.com/portfolio/static/v2/`.
 */

export type Route = 'dashboard' | 'classify'

function parse(hash: string): Route {
  return hash.replace(/^#\/?/, '') === 'classify' ? 'classify' : 'dashboard'
}

class Router {
  current = $state<Route>(parse(location.hash))

  constructor() {
    addEventListener('hashchange', () => {
      this.current = parse(location.hash)
    })
  }

  /** Href for a route. Relative, so it composes with whatever path the app is mounted at. */
  href(route: Route): string {
    return route === 'dashboard' ? '#/' : `#/${route}`
  }
}

export const router = new Router()
