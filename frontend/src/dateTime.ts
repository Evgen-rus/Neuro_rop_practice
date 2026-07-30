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
