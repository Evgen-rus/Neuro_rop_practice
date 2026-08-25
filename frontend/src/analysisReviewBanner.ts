import {
  formatMoscowReviewStamp,
  isMoscowDateTimeOnOrAfter,
  moscowDateTimesOnSameDay,
} from './dateTime.ts'

const CARD_UNCHANGED_STATUSES = new Set(['skip', 'mini'])

export function reviewFromLabel(createdAt?: string | null, now: Date = new Date()): string {
  const stamp = createdAt ? formatMoscowReviewStamp(createdAt, now) : null
  return stamp ? `AI-анализ от ${stamp}` : 'AI-анализ готов'
}

export function laterCheckCopy(input: {
  createdAt?: string | null
  checkedAt?: string | null
  checkStatus?: string | null
  now?: Date
}): string | null {
  const status = String(input.checkStatus || '').trim()
  if (!CARD_UNCHANGED_STATUSES.has(status) || !input.checkedAt || !input.createdAt) return null
  if (!isMoscowDateTimeOnOrAfter(input.checkedAt, input.createdAt)) return null
  if (moscowDateTimesOnSameDay(input.checkedAt, input.now ?? new Date())) {
    return 'Новых фактов для обновления пока не было'
  }
  const stamp = formatMoscowReviewStamp(input.checkedAt, input.now)
  return stamp ? `На проверке ${stamp} новых фактов нет` : null
}
