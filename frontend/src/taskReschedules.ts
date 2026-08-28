import type { DailyTaskResult } from './api'
import { formatMoscowDateTime, parseMoscowDateTime } from './dateTime.ts'

type Reschedule = DailyTaskResult['reschedules'][number]

function rescheduleDate(value: string): Date {
  // Timeline changes preserve Unix seconds as strings; ISO dates remain valid too.
  if (/^\d{10}(?:\.\d+)?$/.test(value)) return new Date(Number(value) * 1000)
  if (/^\d{13}$/.test(value)) return new Date(Number(value))
  return parseMoscowDateTime(value)
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
