import { storedTimezone, saveTimezone } from './timezone'

class TimezoneState {
  preference = $state(storedTimezone())
  browser = $state(Intl.DateTimeFormat().resolvedOptions().timeZone)

  get effective(): string {
    return this.preference === 'browser' ? this.browser : this.preference
  }

  select(value: string): void {
    const storage = {
      getItem: (key: string) => localStorage.getItem(key),
      setItem: (key: string, item: string) => localStorage.setItem(key, item),
    }
    this.preference = saveTimezone(value, storage)
  }

  checkBrowser(): void {
    this.browser = Intl.DateTimeFormat().resolvedOptions().timeZone
  }
}

export const timezone = new TimezoneState()
