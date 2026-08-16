/** What the record-transaction form may offer, and what each choice means.
 *
 * The rules here mirror the server's, so the form never offers what `record_transaction` refuses.
 * They are kept out of the component because they are the part worth testing: a wrong sign in the
 * cash effect and a missing kind filter both survive inspection.
 */

import { ApiError } from './api/client'

/** The transaction types `record_transaction` accepts, in picker order.
 *
 * Corporate actions (`split`, `merger`, `spinoff`, and the rest) are absent deliberately: they
 * post through `record_corporate_action`, which has a preview/apply flow of its own. `reversal`
 * is absent because it is written by reversing an event, never chosen as a type.
 */
export const TRANSACTION_TYPES = [
  'buy',
  'sell',
  'deposit',
  'withdrawal',
  'transfer_in',
  'transfer_out',
  'dividend',
  'interest',
  'fee',
  'tax',
] as const

export type TransactionType = (typeof TRANSACTION_TYPES)[number]

/** Which set of fields a type needs. The type picker comes first because it decides this. */
export type Shape = 'trade' | 'cash' | 'income'

const SHAPES: Record<TransactionType, Shape> = {
  buy: 'trade',
  sell: 'trade',
  deposit: 'cash',
  withdrawal: 'cash',
  transfer_in: 'cash',
  transfer_out: 'cash',
  dividend: 'income',
  interest: 'income',
  fee: 'income',
  tax: 'income',
}

export function shapeFor(type: TransactionType): Shape {
  return SHAPES[type]
}

/** Types a positionless book may post.
 *
 * `reject_security_activity` refuses every `_HOLDING_EVENTS` member on a cash or liability
 * account, which is why `dividend` is missing: a dividend is paid by a security. `interest` is
 * deliberately still here -- it is the canonical cash-account income event, and on a liability
 * book it means interest *received*, not interest charged. Interest charged is a `fee`, which is
 * the server's vocabulary and not something the form renames.
 */
const POSITIONLESS_TYPES = TRANSACTION_TYPES.filter(
  (type) => shapeFor(type) !== 'trade' && type !== 'dividend',
)

/** The types to offer for one account kind.
 *
 * A form that offers `buy` on a savings account only for the server to refuse it is lying about
 * what it accepts. An unrecognized kind takes the positionless list rather than the full one: a
 * kind this client has not caught up with is a book it cannot interpret, and refusing to offer a
 * trade is the safe direction to be wrong in. See API version history 0.6.0.
 */
export function typesForKind(kind: string): TransactionType[] {
  return kind === 'investment' ? [...TRANSACTION_TYPES] : [...POSITIONLESS_TYPES]
}

/** Whether this type may carry a ticker at all, before the account kind is considered. */
export function acceptsTicker(type: TransactionType): boolean {
  return shapeFor(type) === 'trade' || type === 'dividend'
}

/** The entry fields the cash effect reads, exactly as typed. */
export type Entry = {
  type: TransactionType
  amount?: string
  quantity?: string
  unitPrice?: string
  fee?: string
  tax?: string
  settlementAmount?: string
}

/** An empty box means zero, not unknown. A box holding nonsense means unknown. */
function num(value: string | undefined): number {
  if (value === undefined || value.trim() === '') return 0
  return Number(value)
}

/** Whether every figure the caller needs actually parsed. */
function usable(...values: number[]): boolean {
  return values.every((value) => Number.isFinite(value))
}

/**
 * The cash this entry will move, or null when the entry cannot say yet.
 *
 * This is a second implementation of `build_legs`' arithmetic, and the only one in TypeScript.
 * It is *not* a leg preview: it computes the settlement figure and nothing else, because a full
 * preview would duplicate more of `build_legs` and the copy would drift. Its job is to catch a
 * misplaced decimal before it reaches a ledger where the only correction is a reversal that
 * lands on today's date.
 *
 * Floats are acceptable here and nowhere else in this feature. The figure is read by a human as
 * a sanity check and is never sent -- every value on the wire stays the string the user typed.
 *
 * The signs follow `postings.py`. Costs are *added* to what a buy pays and *subtracted* from
 * what a sell receives, because a fee capitalizes into basis either way.
 */
export function cashEffect(entry: Entry): number | null {
  // A supplied settlement wins, exactly as it does in `build_legs`. The preview must state what
  // will be posted, not what would have been posted without it.
  if (entry.settlementAmount !== undefined && entry.settlementAmount.trim() !== '') {
    const supplied = Number(entry.settlementAmount)
    return Number.isFinite(supplied) ? supplied : null
  }

  const fee = num(entry.fee)
  const tax = num(entry.tax)

  if (shapeFor(entry.type) === 'trade') {
    const quantity = Number(entry.quantity)
    const unitPrice = Number(entry.unitPrice)
    if (!entry.quantity?.trim() || !entry.unitPrice?.trim()) return null
    if (!usable(quantity, unitPrice, fee, tax)) return null

    const consideration = quantity * unitPrice
    const costs = fee + tax
    return entry.type === 'buy' ? -(consideration + costs) : consideration - costs
  }

  if (!entry.amount?.trim()) return null
  const amount = Number(entry.amount)
  if (!usable(amount, tax)) return null

  switch (entry.type) {
    case 'deposit':
    case 'transfer_in':
      return amount
    case 'withdrawal':
    case 'transfer_out':
    case 'fee':
    case 'tax':
      // A fee or tax event is a plain outflow. The `tax` field is withholding on income and has
      // no part here -- counting it would charge the same money twice.
      return -amount
    case 'dividend':
    case 'interest':
      // `_income_legs` credits `gross - tax`: income is recorded gross with the withholding
      // split out, and only the net ever reaches the account.
      return amount - tax
    default:
      return null
  }
}

/** Which input a refusal belongs beside, or null for one that belongs to the whole form. */
export type ErrorField = 'amount' | 'quantity' | 'ticker' | 'settlement_amount' | null

export type ShownError = { field: ErrorField; message: string }

/** A written sentence per refusal, shown at the field it concerns.
 *
 * The API's own messages are written for an agent branching on JSON, not for someone mid-entry
 * with a broker statement in hand. `AccountSettings` sets the precedent with
 * `portfolio_name_exists`. Each sentence says what to do next, because a refusal that only
 * restates the rule leaves the user where they were.
 */
const SENTENCES: Record<string, ShownError> = {
  insufficient_cash: {
    field: 'amount',
    message: 'This account does not hold that much cash. Record the deposit that funded it first.',
  },
  insufficient_position: {
    field: 'quantity',
    message: 'This account holds fewer shares than that. Check the quantity against the holdings tab.',
  },
  currency_mismatch: {
    field: 'ticker',
    message:
      'This instrument is not quoted in the account’s currency. Hold it in an account of its own currency.',
  },
  market_data_unavailable: {
    field: 'ticker',
    message: 'No price data could be found for this symbol. It may be delisted or misspelled.',
  },
  not_a_securities_account: {
    field: 'ticker',
    message: 'This account holds no securities, so a transaction here cannot name an instrument.',
  },
  journal_out_of_balance: {
    field: 'settlement_amount',
    message: 'This settlement amount does not balance the event. Leave it empty to use the computed figure.',
  },
  invalid_amount: {
    field: 'amount',
    message: 'Enter an amount above zero. The transaction type already sets the direction.',
  },
  missing_field: {
    field: null,
    message: 'One required field is empty. Fill in every field for this transaction type.',
  },
  idempotency_conflict: {
    field: null,
    message: 'This entry was already submitted with different values. Close the form and reopen it.',
  },
  already_reversed: {
    field: null,
    message: 'This transaction was already reversed. Post a replacement instead of reversing again.',
  },
  cannot_reverse_a_reversal: {
    field: null,
    message: 'A reversal cannot be reversed. Post a replacement transaction instead.',
  },
  reverse_the_transfer_instead: {
    field: null,
    message:
      'This is one half of a transfer. Reverse the transfer itself so both accounts unwind together.',
  },
}

/** The sentence and field for one failure.
 *
 * An unrecognized code keeps the server's own message. Writing a friendly sentence for a code
 * this client has never seen would be inventing what went wrong, and a wrong explanation is
 * worse than a terse true one.
 */
export function errorFor(err: unknown): ShownError {
  const known = err instanceof ApiError ? SENTENCES[err.code] : undefined
  if (known) return known
  return { field: null, message: err instanceof Error ? err.message : String(err) }
}
