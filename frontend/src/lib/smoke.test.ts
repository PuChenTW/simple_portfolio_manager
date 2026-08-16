import { describe, expect, it } from 'vitest'

import { safeWidth } from './format'

// The harness proof, not a coverage effort. It imports a real project module through the same
// relative path the app uses, so a broken TypeScript or resolution setup fails here rather than
// inside the first test that matters.
describe('test runner', () => {
  it('resolves TypeScript modules by project-relative path', () => {
    expect(safeWidth('42')).toBe(42)
  })

  it('runs against the same source the app imports', () => {
    // A negative weight is real: a group whose debts exceed its assets produces one.
    expect(safeWidth('-3')).toBe(0)
  })
})
