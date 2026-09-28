export const MOSCOW_TIME_ZONE = 'Europe/Moscow'

type DateTimeValue = string | number | Date

const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/
const DATE_TIME_WITHOUT_ZONE_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

export function parseMoscowDateTime(value: DateTimeValue): Date {
  if (value instanceof Date || typeof value === 'number') return new Date(value)
  const normalized = DATE_ONLY_RE.test(value)
    ? `${value}T00:00:00+03:00`
    : DATE_TIME_WITHOUT_ZONE_RE.test(value)
      ? `${value.replace(' ', 'T')}+03:00`
      : value
  return new Date(normalized)
}

export function formatMoscowDateTime(
  value: DateTimeValue,
  options: Intl.DateTimeFormatOptions,
): string | null {
  const parsed = parseMoscowDateTime(value)
  if (Number.isNaN(parsed.getTime())) return null
  return new Intl.DateTimeFormat('ru-RU', {
    ...options,
    timeZone: MOSCOW_TIME_ZONE,
  }).format(parsed)
}

export function moscowDateParts(value: DateTimeValue = new Date()) {
  const parsed = parseMoscowDateTime(value)
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: MOSCOW_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(parsed)
  const mapped = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return {
    year: Number(mapped.year),
    month: Number(mapped.month),
    day: Number(mapped.day),
  }
}

export function moscowDateInputValue(value: DateTimeValue = new Date()): string {
  const { year, month, day } = moscowDateParts(value)
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

const SHORT_DATE_RE = /^(\d{2})\.(\d{2})\.(\d{4})$/

/** `дд.мм.гггг` в ISO-значение, которым API и `min`/`max` оперируют.
 *  Возвращает `null`, если строка не дата или если такого дня в календаре нет:
 *  `31.02.2026` — опечатка, а не 3 марта. */
export function shortDateToIso(value: string): string | null {
  const match = SHORT_DATE_RE.exec(value.trim())
  if (!match) return null
  const [, day, month, year] = match
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)))
  if (
    date.getUTCFullYear() !== Number(year)
    || date.getUTCMonth() !== Number(month) - 1
    || date.getUTCDate() !== Number(day)
  ) return null
  return `${year}-${month}-${day}`
}

/** ISO-значение в `дд.мм.гггг` для показа. */
export function isoToShortDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  return match ? `${match[3]}.${match[2]}.${match[1]}` : value
}

/** Первый день месяца по ISO-значению даты: `2026-09-28` → `2026-09-01`. */
export function moscowMonthStart(isoDate: string): string {
  const match = /^(\d{4})-(\d{2})-\d{2}$/.exec(isoDate)
  return match ? `${match[1]}-${match[2]}-01` : isoDate
}

export function formatMoscowReviewStamp(value: DateTimeValue, now: DateTimeValue = new Date()): string | null {
  const date = formatMoscowDateTime(value, { day: 'numeric', month: 'long' })
  const time = formatMoscowDateTime(value, { hour: '2-digit', minute: '2-digit' })
  if (!date || !time) return null
  const valueYear = moscowDateParts(value).year
  const nowYear = moscowDateParts(now).year
  const yearPart = valueYear !== nowYear ? ` ${valueYear}` : ''
  return `${date}${yearPart}, ${time}`
}

export function moscowDateTimesOnSameDay(left: DateTimeValue, right: DateTimeValue = new Date()): boolean {
  const a = moscowDateParts(left)
  const b = moscowDateParts(right)
  return a.year === b.year && a.month === b.month && a.day === b.day
}

export function isMoscowDateTimeOnOrAfter(value: DateTimeValue, reference: DateTimeValue): boolean {
  const left = parseMoscowDateTime(value).getTime()
  const right = parseMoscowDateTime(reference).getTime()
  if (Number.isNaN(left) || Number.isNaN(right)) return false
  return left >= right
}
