import type { DailyTaskResult } from './api'
import { formatMoscowDateTime, moscowDateParts, parseMoscowDateTime } from './dateTime.ts'

type Reschedule = DailyTaskResult['reschedules'][number]
type DateTimeValue = string | number | Date

function rescheduleDate(value: string): Date {
  // Timeline changes preserve Unix seconds as strings; ISO dates remain valid too.
  if (/^\d{10}(?:\.\d+)?$/.test(value)) return new Date(Number(value) * 1000)
  if (/^\d{13}$/.test(value)) return new Date(Number(value))
  return parseMoscowDateTime(value)
}

function parsedRescheduleDate(value?: string | null): Date | null {
  if (!value?.trim()) return null
  const parsed = rescheduleDate(value.trim())
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function moscowDayDelta(left: Date, right: DateTimeValue): number {
  const a = moscowDateParts(left)
  const b = moscowDateParts(right)
  return a.year - b.year || a.month - b.month || a.day - b.day
}

export function rescheduleFromDueToday(fromDeadline?: string | null, now: DateTimeValue = new Date()): boolean {
  const parsed = parsedRescheduleDate(fromDeadline)
  return parsed !== null && moscowDayDelta(parsed, now) === 0
}

export function rescheduleFromDueOnOrBeforeToday(fromDeadline?: string | null, now: DateTimeValue = new Date()): boolean {
  const parsed = parsedRescheduleDate(fromDeadline)
  return parsed !== null && moscowDayDelta(parsed, now) <= 0
}

export function formatRescheduleDeadline(value?: string | null): string {
  if (!value?.trim()) return 'Без срока'
  return formatMoscowDateTime(rescheduleDate(value.trim()), {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  }) || 'Срок не определён'
}

export function newestReschedules(changes: Reschedule[]): Reschedule[] {
  return [...changes].reverse().sort((a, b) => {
    const left = rescheduleDate(a.occurred_at).getTime()
    const right = rescheduleDate(b.occurred_at).getTime()
    return (Number.isNaN(right) ? -Infinity : right) - (Number.isNaN(left) ? -Infinity : left)
  })
}
