import { describe, expect, it } from 'vitest'

import { ApiError } from './api/client'
import { errorFor, shapeFor, typesForKind, TRANSACTION_TYPES } from './transactions'

describe('shapeFor', () => {
  it('maps buys and sells to the trade shape', () => {
    expect(shapeFor('buy')).toBe('trade')
    expect(shapeFor('sell')).toBe('trade')
  })

  it('maps the four cash movements to the cash shape', () => {
    expect(shapeFor('deposit')).toBe('cash')
    expect(shapeFor('withdrawal')).toBe('cash')
    expect(shapeFor('transfer_in')).toBe('cash')
    expect(shapeFor('transfer_out')).toBe('cash')
  })

  it('maps income and costs to the income shape', () => {
    expect(shapeFor('dividend')).toBe('income')
    expect(shapeFor('interest')).toBe('income')
    expect(shapeFor('fee')).toBe('income')
    expect(shapeFor('tax')).toBe('income')
  })

  it('covers every type the picker offers', () => {
    for (const type of TRANSACTION_TYPES) {
      expect(shapeFor(type)).toBeDefined()
    }
  })
})

describe('typesForKind', () => {
  it('offers every type on an investment account', () => {
    expect(typesForKind('investment')).toEqual([...TRANSACTION_TYPES])
  })

  it('offers no trade on a cash account', () => {
    const types = typesForKind('cash')
    expect(types).not.toContain('buy')
    expect(types).not.toContain('sell')
  })

  it('offers no trade on a liability account', () => {
    const types = typesForKind('liability')
    expect(types).not.toContain('buy')
    expect(types).not.toContain('sell')
  })

  // `reject_security_activity` refuses a dividend on a positionless book: a dividend is paid by
  // a security. Interest is the canonical cash-account income event and stays.
  it('offers no dividend on a positionless book, but keeps interest', () => {
    for (const kind of ['cash', 'liability'] as const) {
      expect(typesForKind(kind)).not.toContain('dividend')
      expect(typesForKind(kind)).toContain('interest')
    }
  })

  it('keeps the cash movements on every kind', () => {
    for (const kind of ['investment', 'cash', 'liability'] as const) {
      const types = typesForKind(kind)
      for (const type of ['deposit', 'withdrawal', 'transfer_in', 'transfer_out'] as const) {
        expect(types).toContain(type)
      }
    }
  })

  // An unknown kind is a book this client cannot interpret. See API version history 0.6.0.
  it('falls back to the positionless list for an unknown kind', () => {
    const types = typesForKind('something-new')
    expect(types).not.toContain('buy')
    expect(types).toContain('deposit')
  })
})

describe('errorFor', () => {
  it('puts insufficient cash at the amount field', () => {
    const shown = errorFor(new ApiError(422, 'insufficient_cash', 'raw agent text'))
    expect(shown.field).toBe('amount')
    expect(shown.message).not.toContain('raw agent text')
  })

  it('puts insufficient position at the quantity field', () => {
    expect(errorFor(new ApiError(422, 'insufficient_position', '')).field).toBe('quantity')
  })

  it('puts both ticker refusals at the ticker field', () => {
    expect(errorFor(new ApiError(422, 'currency_mismatch', '')).field).toBe('ticker')
    expect(errorFor(new ApiError(503, 'market_data_unavailable', '')).field).toBe('ticker')
  })

  it('puts an unbalanced settlement at the settlement field', () => {
    expect(errorFor(new ApiError(422, 'journal_out_of_balance', '')).field).toBe(
      'settlement_amount',
    )
  })

  it('writes a sentence for every code that reaches this form', () => {
    const codes = [
      'insufficient_cash',
      'insufficient_position',
      'currency_mismatch',
      'market_data_unavailable',
      'idempotency_conflict',
      'not_a_securities_account',
      'journal_out_of_balance',
      'already_reversed',
      'cannot_reverse_a_reversal',
      'reverse_the_transfer_instead',
    ]
    for (const code of codes) {
      const shown = errorFor(new ApiError(422, code, 'raw'))
      expect(shown.message).toMatch(/\.$/)
      expect(shown.message).not.toBe('raw')
    }
  })

  // An unrecognized code must still say something true, and the server's own text is the only
  // thing that can. A written sentence for a code nobody anticipated would be invented.
  it('falls back to the server message for an unknown code', () => {
    const shown = errorFor(new ApiError(500, 'something_new', 'the server said this'))
    expect(shown.message).toBe('the server said this')
    expect(shown.field).toBeNull()
  })

  it('falls back to a plain Error message', () => {
    expect(errorFor(new Error('network died')).message).toBe('network died')
  })
})
