import { describe, expect, it } from 'vitest'
import { localTimeOptions, resolveTime, timeFields, readTimezone, saveTimezone } from './timezone'

describe('timezone preference', () => {
  it('defaults to browser mode and remembers a named timezone', () => {
    const values = new Map<string, string>()
    const storage = { getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) } }
    expect(readTimezone(storage)).toBe('browser')
    saveTimezone('Asia/Taipei', storage)
    expect(readTimezone(storage)).toBe('Asia/Taipei')
    saveTimezone('browser', storage)
    expect(readTimezone(storage)).toBe('browser')
  })
})

describe('transaction time input', () => {
  it('converts a Taipei morning to the preceding UTC date', () => {
    expect(resolveTime({ date: '2026-09-17', time: '07:00', timezone: 'Asia/Taipei' }))
      .toEqual({ timestamp: '2026-09-16T23:00:00Z', error: null })
  })
  it('rejects a nonexistent daylight-saving time', () => {
    expect(localTimeOptions({ date: '2026-03-08', time: '02:30', timezone: 'America/New_York' }))
      .toEqual([])
  })
  it('requires a choice for a repeated daylight-saving time', () => {
    const input = { date: '2026-11-01', time: '01:30', timezone: 'America/New_York' }
    expect(localTimeOptions(input).map(option => option.offset)).toEqual(['-04:00', '-05:00'])
    expect(resolveTime(input).timestamp).toBeNull()
    expect(resolveTime({ ...input, offset: '-05:00' }).timestamp).toBe('2026-11-01T06:30:00Z')
  })
  it('initializes minute precision in the selected timezone', () => {
    expect(timeFields('Asia/Taipei', new Date('2026-09-16T23:23:45Z')))
      .toEqual({ date: '2026-09-17', time: '07:23' })
  })
})
