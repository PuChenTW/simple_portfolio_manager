/** Splitting a snapshot rebuild into requests the browser can actually finish.
 *
 * The rebuild endpoint is synchronous: it replays the journal once per date inside the request.
 * A multi-year range in one call holds the connection open long past any browser timeout, and
 * the work then continues server-side with nothing watching it. So the client sends one request
 * per calendar month and reports progress between them.
 *
 * Calendar months rather than a round number of days: `cache.py` widens every history fetch to
 * month boundaries and stores whole months, so a month-aligned request asks for exactly what the
 * cache stores instead of straddling four buckets. It also gives the progress display a name a
 * reader recognizes -- "March 2024" rather than "chunk 7 of 23".
 *
 * Dates are handled as `YYYY-MM-DD` strings throughout. Constructing a `Date` from one parses it
 * as UTC midnight while its getters read local time, so a machine west of Greenwich silently
 * reports the previous day. Strings avoid the whole class of bug.
 */

export type Chunk = {
  /** Inclusive `YYYY-MM-DD` bounds, never crossing a month boundary. */
  start: string
  end: string
  /** The month this chunk covers, for display: "March 2024". */
  label: string
}

const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

function parts(iso: string): [number, number, number] {
  const [year, month, day] = iso.split('-').map(Number)
  return [year, month, day]
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function iso(year: number, month: number, day: number): string {
  return `${year}-${pad(month)}-${pad(day)}`
}

/** Last day of a month, via day 0 of the next one. UTC so the value cannot shift by a timezone. */
function lastDayOfMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate()
}

/** Today as `YYYY-MM-DD` in the viewer's own timezone, which is the date they mean by "today". */
export function today(): string {
  const now = new Date()
  return iso(now.getFullYear(), now.getMonth() + 1, now.getDate())
}

/** `YYYY-MM-DD` for the first day of the year the given date falls in. */
export function startOfYear(date: string): string {
  return iso(parts(date)[0], 1, 1)
}

/** Move a date by a number of days. Uses UTC arithmetic, then formats from UTC getters. */
export function addDays(date: string, days: number): string {
  const [year, month, day] = parts(date)
  const shifted = new Date(Date.UTC(year, month - 1, day + days))
  return iso(shifted.getUTCFullYear(), shifted.getUTCMonth() + 1, shifted.getUTCDate())
}

/** How many dates the range covers, inclusive of both ends. Zero when the range is inverted. */
export function dayCount(start: string, end: string): number {
  const [sy, sm, sd] = parts(start)
  const [ey, em, ed] = parts(end)
  const from = Date.UTC(sy, sm - 1, sd)
  const to = Date.UTC(ey, em - 1, ed)
  if (to < from) return 0
  return Math.round((to - from) / 86_400_000) + 1
}

/**
 * Split an inclusive range into one chunk per calendar month.
 *
 * The first and last chunks are clipped to the requested bounds, so a range starting mid-month
 * does not silently rebuild the days before it. An inverted range yields nothing rather than
 * throwing: the caller disables its button on an empty list, which is the same check.
 */
export function monthChunks(start: string, end: string): Chunk[] {
  if (dayCount(start, end) === 0) return []

  const chunks: Chunk[] = []
  const [endYear, endMonth] = parts(end)
  let [year, month] = parts(start)
  let cursor = start

  while (year < endYear || (year === endYear && month <= endMonth)) {
    const monthEnd = iso(year, month, lastDayOfMonth(year, month))
    // Clip the final chunk to the requested end rather than running to the month's last day.
    const chunkEnd = year === endYear && month === endMonth ? end : monthEnd
    chunks.push({ start: cursor, end: chunkEnd, label: `${MONTHS[month - 1]} ${year}` })

    month += 1
    if (month > 12) {
      month = 1
      year += 1
    }
    cursor = iso(year, month, 1)
  }

  return chunks
}
