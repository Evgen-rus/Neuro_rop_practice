import type { AiSpendAnalytics, AiSpendDailyPoint, AiSpendPeriodPreset } from './api'

export type SpendChartMetric = 'cost' | 'calls' | 'tokens'
export type SpendKindFilter = 'all' | 'full_analysis' | 'quick_help' | 'scripts' | 'transcription' | 'other'

export const SPEND_PERIOD_PRESETS: { id: AiSpendPeriodPreset; label: string }[] = [
  { id: '7', label: '7 дней' },
  { id: '30', label: '30 дней' },
  { id: 'custom', label: 'Свой период' },
]

export const SPEND_CHART_METRICS: { id: SpendChartMetric; label: string }[] = [
  { id: 'cost', label: 'Расходы' },
  { id: 'calls', label: 'Вызовы' },
  { id: 'tokens', label: 'Токены' },
]

export function buildAiSpendPeriodQuery(
  preset: AiSpendPeriodPreset,
  fromDate: string,
  toDate: string,
): URLSearchParams {
  const query = new URLSearchParams({ preset })
  if (preset === 'custom' && fromDate && toDate) {
    query.set('from', fromDate)
    query.set('to', toDate)
  }
  return query
}

export function buildAiSpendEventsQuery(options: {
  preset: AiSpendPeriodPreset
  fromDate: string
  toDate: string
  q?: string
  kindGroup?: string
  status?: string
  attention?: string
  page?: number
  pageSize?: number
}): URLSearchParams {
  const query = buildAiSpendPeriodQuery(options.preset, options.fromDate, options.toDate)
  if (options.q?.trim()) query.set('q', options.q.trim())
  if (options.kindGroup && options.kindGroup !== 'all') query.set('kind_group', options.kindGroup)
  if (options.status) query.set('status', options.status)
  if (options.attention) query.set('attention', options.attention)
  if (options.page && options.page > 1) query.set('page', String(options.page))
  if (options.pageSize) query.set('page_size', String(options.pageSize))
  return query
}

export function spendChartSeries(
  analytics: AiSpendAnalytics | null,
  kindFilter: SpendKindFilter,
): AiSpendDailyPoint[] {
  if (!analytics) return []
  if (kindFilter === 'all') return analytics.daily_series
  const group = analytics.kind_groups.find((item) => item.id === kindFilter)
  return group?.daily || analytics.daily_series.map((item) => ({
    ...item,
    estimated_cost_rub: 0,
    estimated_cost_rub_label: '~0 ₽',
    paid_calls: 0,
    paid_calls_label: '0 платных вызовов',
    total_tokens: 0,
    total_tokens_label: '0',
    unknown_cost_calls: 0,
  }))
}

export function spendChartValue(point: AiSpendDailyPoint, metric: SpendChartMetric): number {
  if (metric === 'calls') return point.paid_calls
  if (metric === 'tokens') return point.total_tokens
  return point.estimated_cost_rub ?? 0
}

export function formatSpendDelta(value: number | null | undefined): { text: string; tone: 'up' | 'down' | 'flat' } | null {
  if (value == null) return null
  if (value === 0) return { text: '0%', tone: 'flat' }
  const magnitude = Math.abs(value)
  const rounded = Math.abs(magnitude - Math.round(magnitude)) < 0.05
    ? String(Math.round(magnitude))
    : magnitude.toFixed(1).replace('.', ',')
  return {
    text: `${value > 0 ? '↑' : '↓'} ${rounded}%`,
    tone: value > 0 ? 'up' : 'down',
  }
}

export function shareWidth(share: number | null | undefined): string {
  if (share == null || share <= 0) return '0%'
  return `${Math.min(share, 100)}%`
}

export const SPEND_CHART_WIDTH = 800
export const SPEND_CHART_HEIGHT = 140
export const SPEND_CHART_PAD_X = 10

export function spendChartIndexFromSvgX(
  svgX: number,
  pointCount: number,
  width = SPEND_CHART_WIDTH,
  padX = SPEND_CHART_PAD_X,
): number {
  if (pointCount <= 1) return 0
  const innerW = Math.max(width - padX * 2, 1)
  const ratio = (svgX - padX) / innerW
  return Math.max(0, Math.min(pointCount - 1, Math.round(ratio * (pointCount - 1))))
}

export function spendShiftIsoDate(iso: string, days: number): string {
  const [year, month, day] = iso.split('-').map(Number)
  const utc = new Date(Date.UTC(year, month - 1, day + days))
  return [
    utc.getUTCFullYear(),
    String(utc.getUTCMonth() + 1).padStart(2, '0'),
    String(utc.getUTCDate()).padStart(2, '0'),
  ].join('-')
}

export function spendDayRelativeLabel(date: string, today: string): string | null {
  if (date === today) return 'Сегодня'
  if (date === spendShiftIsoDate(today, -1)) return 'Вчера'
  if (date === spendShiftIsoDate(today, -2)) return 'Позавчера'
  return null
}

export function spendDayTitle(date: string, today: string, dateLabel: string): string {
  const relative = spendDayRelativeLabel(date, today)
  return relative ? `${relative} · ${dateLabel}` : dateLabel
}
