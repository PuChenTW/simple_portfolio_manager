import { describe, expect, it } from 'vitest'

import { ApiError } from './api/client'
import {
  cashEffect,
  errorFor,
  shapeFor,
  staleFrom,
  typesForKind,
  TRANSACTION_TYPES,
} from './transactions'

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

// The line this covers is a second implementation of `build_legs`' arithmetic in TypeScript,
// which makes it the one piece of the record form that inspection will not catch when it is
// wrong. The expected values below come from `postings.py:139-143` and `_income_legs`.
describe('cashEffect', () => {
  describe('a trade', () => {
    it('takes consideration plus costs out for a buy', () => {
      // 10 x 100 = 1000, +5 fee +2 tax. Costs are *added* to what a buy pays.
      expect(cashEffect({ type: 'buy', quantity: '10', unitPrice: '100', fee: '5', tax: '2' })).toBe(
        -1007,
      )
    })

    it('brings consideration minus costs in for a sell', () => {
      // Costs are *subtracted* from what a sell receives. Getting this sign backwards is the
      // plausible mistake, and it is why both directions are asserted with non-zero costs.
      expect(
        cashEffect({ type: 'sell', quantity: '10', unitPrice: '100', fee: '5', tax: '2' }),
      ).toBe(993)
    })

    it('handles zero fee and zero tax in both directions', () => {
      expect(cashEffect({ type: 'buy', quantity: '10', unitPrice: '100' })).toBe(-1000)
      expect(cashEffect({ type: 'sell', quantity: '10', unitPrice: '100' })).toBe(1000)
    })

    it('treats an empty cost box as zero rather than as unknown', () => {
      expect(
        cashEffect({ type: 'buy', quantity: '2', unitPrice: '50', fee: '', tax: '' }),
      ).toBe(-100)
    })

    it('applies a fee with no tax, and a tax with no fee', () => {
      expect(cashEffect({ type: 'buy', quantity: '1', unitPrice: '100', fee: '3' })).toBe(-103)
      expect(cashEffect({ type: 'sell', quantity: '1', unitPrice: '100', tax: '3' })).toBe(97)
    })

    it('handles a fractional quantity', () => {
      expect(cashEffect({ type: 'buy', quantity: '0.5', unitPrice: '200' })).toBe(-100)
    })

    // The preview states what will actually be posted, and `build_legs` uses the supplied
    // settlement over the computed one. Showing the computed figure here would state something
    // the server is about to contradict.
    it('reports a supplied settlement amount instead of the computed one', () => {
      expect(
        cashEffect({
          type: 'buy',
          quantity: '10',
          unitPrice: '100',
          fee: '5',
          settlementAmount: '-1004.37',
        }),
      ).toBe(-1004.37)
    })

    it('reports nothing until quantity and price are both present', () => {
      expect(cashEffect({ type: 'buy', quantity: '10' })).toBeNull()
      expect(cashEffect({ type: 'buy', unitPrice: '100' })).toBeNull()
      expect(cashEffect({ type: 'buy' })).toBeNull()
    })

    it('reports nothing for an unparseable figure', () => {
      expect(cashEffect({ type: 'buy', quantity: 'ten', unitPrice: '100' })).toBeNull()
    })
  })

  describe('a cash movement', () => {
    it('adds cash for a deposit and a transfer in', () => {
      expect(cashEffect({ type: 'deposit', amount: '500' })).toBe(500)
      expect(cashEffect({ type: 'transfer_in', amount: '500' })).toBe(500)
    })

    it('removes cash for a withdrawal and a transfer out', () => {
      expect(cashEffect({ type: 'withdrawal', amount: '500' })).toBe(-500)
      expect(cashEffect({ type: 'transfer_out', amount: '500' })).toBe(-500)
    })
  })

  describe('income and costs', () => {
    // `_income_legs` credits cash with `gross - tax`: income is recorded gross with the
    // withholding split out, and only the net actually arrives.
    it('credits income net of withholding tax', () => {
      expect(cashEffect({ type: 'dividend', amount: '100', tax: '30' })).toBe(70)
      expect(cashEffect({ type: 'interest', amount: '100', tax: '30' })).toBe(70)
    })

    it('credits the full amount when no tax is withheld', () => {
      expect(cashEffect({ type: 'dividend', amount: '100' })).toBe(100)
    })

    // A fee and a tax event are `_cash_pair` outflows. The `tax` *field* is withholding on
    // income and has no part in them -- adding it here would double-count the same money.
    it('removes cash for a fee and for a tax, ignoring the withholding field', () => {
      expect(cashEffect({ type: 'fee', amount: '25' })).toBe(-25)
      expect(cashEffect({ type: 'tax', amount: '25', tax: '9' })).toBe(-25)
    })
  })

  it('reports nothing when the amount is missing', () => {
    expect(cashEffect({ type: 'deposit' })).toBeNull()
    expect(cashEffect({ type: 'dividend', amount: '' })).toBeNull()
  })
})

describe('staleFrom', () => {
  it('reports the affected date when it is in the past', () => {
    expect(staleFrom('2026-06-01T00:00:00', '2026-08-16')).toBe('2026-06-01')
  })

  // Today has no stored snapshot to be wrong yet, so a notice would be unactionable.
  it('reports nothing for today', () => {
    expect(staleFrom('2026-08-16T00:00:00', '2026-08-16')).toBeNull()
  })

  it('reports nothing for a future date', () => {
    expect(staleFrom('2026-09-01T00:00:00', '2026-08-16')).toBeNull()
  })

  // A reversal is dated today but undoes an event that may not have been. The original's date
  // is what snapshots went wrong from, so that is what the caller passes.
  it('takes the date it is given, not the reversal date', () => {
    expect(staleFrom('2026-06-01T09:48:05.860775Z', '2026-08-16')).toBe('2026-06-01')
  })
})
