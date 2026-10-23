import { Temporal } from '@js-temporal/polyfill'

const TIMEZONE_KEY = 'portfolio.timezone'
let preference: string | null = null

export function storedTimezone(): string {
  try { return readTimezone(localStorage) } catch { return 'browser' }
}

export function effectiveTimezone(): string {
  const value = preference ?? storedTimezone()
  return value === 'browser' ? Intl.DateTimeFormat().resolvedOptions().timeZone : value
}
type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>

export function readTimezone(storage: PreferenceStorage): string {
  try {
    const value = storage.getItem(TIMEZONE_KEY)
    return value === 'browser' ? value : canonicalTimezone(value ?? '') ?? 'browser'
  } catch {
    return 'browser'
  }
}

export function saveTimezone(value: string, storage: PreferenceStorage): string {
  const canonical = value === 'browser' ? value : canonicalTimezone(value)
  if (!canonical) throw new Error('Use an IANA timezone name.')
  preference = canonical
  try {
    storage.setItem(TIMEZONE_KEY, canonical)
  } catch {
    // Storage can be unavailable in private browsing. The in-memory preference still applies.
  }
  return canonical
}

type LocalTime = { date: string; time: string; timezone: string; offset?: string }

export function timeFields(timezone: string, now = new Date()) {
  const value = Temporal.Instant.from(now.toISOString()).toZonedDateTimeISO(timezone)
  return { date: value.toPlainDate().toString(), time: value.toPlainTime().toString().slice(0, 5) }
}

export function localTimeOptions(input: LocalTime): { timestamp: string; offset: string }[] {
  if (!input.date || !input.time) return []
  try {
    const local = Temporal.PlainDateTime.from(`${input.date}T${input.time}:00`)
    const earlier = local.toZonedDateTime(input.timezone, { disambiguation: 'earlier' })
    const later = local.toZonedDateTime(input.timezone, { disambiguation: 'later' })
    const options = earlier.equals(later) ? [earlier] : [earlier, later]
    return options.filter(value => value.toPlainDateTime().equals(local))
      .map(value => ({ timestamp: value.toInstant().toString(), offset: value.offset }))
  } catch {
    return []
  }
}

export function resolveTime(input: LocalTime) {
  const options = localTimeOptions(input)
  if (!options.length) return { timestamp: null, error: 'Enter a valid date and time in this timezone.' }
  const selected = options.length === 1 ? options[0] : options.find(value => value.offset === input.offset)
  if (!selected) return { timestamp: null, error: 'This time occurs twice. Select a UTC offset.' }
  return { timestamp: selected.timestamp, error: null }
}

export function validTimezone(value: string): boolean {
  return canonicalTimezone(value) !== null
}

function canonicalTimezone(value: string): string | null {
  if (/^[+-]/.test(value)) return null
  try {
    return new Intl.DateTimeFormat('en', { timeZone: value }).resolvedOptions().timeZone
  } catch {
    return null
  }
}
