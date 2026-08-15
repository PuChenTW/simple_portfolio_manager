// The API sends every financial value as a decimal *string* to avoid binary float error.
// Parsing to Number is safe for display only -- never for arithmetic that feeds another value.
// Any total shown here must come from a field the server computed.

const MONEY_FRACTION_DIGITS = 0

export function money(value: string | null | undefined, currency: string): string {
  if (value == null) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency,
    maximumFractionDigits: MONEY_FRACTION_DIGITS,
  }).format(parsed)
}

/**
 * Money for a tight slot (a donut hole, a stat tile): 1.2M rather than 1,234,567. The exact
 * figure must stay readable elsewhere on screen -- this trades precision for fit.
 */
export function compactMoney(value: string | null | undefined, currency: string): string {
  if (value == null) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency,
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(parsed)
}

export function percent(value: string | null | undefined, digits = 1): string {
  if (value == null) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return `${parsed.toFixed(digits)}%`
}

export function quantity(value: string | null | undefined): string {
  if (value == null) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(parsed)
}

export function isNegative(value: string | null | undefined): boolean {
  return value != null && Number(value) < 0
}

/** Bar widths must never go negative: debts can exceed assets and weights can invert. */
export function safeWidth(value: string | null | undefined): number {
  const parsed = Number(value ?? Number.NaN)
  if (!Number.isFinite(parsed) || parsed <= 0) return 0
  return Math.min(parsed, 100)
}

export function shortDate(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString()
}
