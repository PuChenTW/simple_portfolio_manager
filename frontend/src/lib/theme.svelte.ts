/** The reader's theme choice.
 *
 * Three states, not two. `system` is the default and follows the OS, which is what a machine
 * that switches at sunset expects; `light` and `dark` are explicit overrides that must win over
 * the preference in both directions. A two-state toggle cannot express "follow the system"
 * once it has been left, so the choice would be sticky in a way the reader never asked for.
 *
 * The attribute this writes is what `app.css` keys on: bare `:root` carries the light palette,
 * `:root:not([data-theme='light'])` under `prefers-color-scheme: dark` carries the system case,
 * and `:root[data-theme='dark']` carries the explicit one.
 */

export type Theme = 'system' | 'light' | 'dark'

const STORAGE_KEY = 'portfolio.theme'

const THEMES: Theme[] = ['system', 'light', 'dark']

function isTheme(value: string | null): value is Theme {
  return value !== null && (THEMES as string[]).includes(value)
}

function readStored(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return isTheme(stored) ? stored : 'system'
  } catch {
    return 'system' // Private browsing or a blocked origin; following the system is the default.
  }
}

class ThemeState {
  choice = $state<Theme>(readStored())

  /** Tracks the OS preference so the label can say what `system` currently resolves to. */
  #prefersDark = $state(false)

  constructor() {
    const query = matchMedia('(prefers-color-scheme: dark)')
    this.#prefersDark = query.matches
    query.addEventListener('change', (event) => {
      this.#prefersDark = event.matches
    })
    this.#apply()
  }

  /** What is actually on screen right now, with `system` resolved. */
  get resolved(): 'light' | 'dark' {
    if (this.choice === 'system') return this.#prefersDark ? 'dark' : 'light'
    return this.choice
  }

  set(choice: Theme): void {
    this.choice = choice
    this.#apply()
    try {
      // `system` is the default, so it is stored as the absence of a choice rather than as a
      // value -- a reader who returns to following the OS should not carry a stale override.
      if (choice === 'system') localStorage.removeItem(STORAGE_KEY)
      else localStorage.setItem(STORAGE_KEY, choice)
    } catch {
      /* Remembering is a convenience, never a requirement. */
    }
  }

  /** Step through the three states, for the single-button toggle. */
  cycle(): void {
    this.set(THEMES[(THEMES.indexOf(this.choice) + 1) % THEMES.length])
  }

  #apply(): void {
    const root = document.documentElement
    if (this.choice === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', this.choice)
  }
}

export const theme = new ThemeState()
