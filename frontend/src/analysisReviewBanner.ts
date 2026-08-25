import {
  formatMoscowReviewStamp,
  isMoscowDateTimeOnOrAfter,
} from './dateTime.ts'

const CARD_UNCHANGED_STATUSES = new Set(['skip', 'mini'])

type ReviewBannerInput = {
  createdAt?: string | null
  checkedAt?: string | null
  checkStatus?: string | null
  now?: Date
}

// Skip/mini после отчёта: система уже проверяла позже, но JSON не переписывала.
function laterUnchangedCheck(input: ReviewBannerInput): boolean {
  const status = String(input.checkStatus || '').trim()
  if (!CARD_UNCHANGED_STATUSES.has(status) || !input.checkedAt || !input.createdAt) return false
  return isMoscowDateTimeOnOrAfter(input.checkedAt, input.createdAt)
}

export function reviewHeadlineAt(input: ReviewBannerInput): string | null {
  return laterUnchangedCheck(input) ? input.checkedAt ?? null : input.createdAt ?? null
}

export function reviewFromLabel(createdAt?: string | null, now: Date = new Date()): string {
  const stamp = createdAt ? formatMoscowReviewStamp(createdAt, now) : null
  return stamp ? `AI-анализ от ${stamp}` : 'AI-анализ готов'
}

export function laterCheckCopy(input: ReviewBannerInput): string | null {
  if (!laterUnchangedCheck(input) || !input.createdAt) return null
  const stamp = formatMoscowReviewStamp(input.createdAt, input.now)
  return stamp ? `С ${stamp} новых фактов для AI-анализа не было` : null
}
