import type { AiSpendAnalytics, AiSpendBreakdownRow, AiSpendDailyPoint } from './api'

export type SpendChartMetric = 'cost' | 'calls' | 'tokens'
export type SpendKindFilter = 'all' | 'full_analysis' | 'quick_help' | 'scripts' | 'transcription' | 'other'

/** Относительных пресетов нет: период всегда задан датами. Серверные
 *  `7`/`30` остались живыми для HTTP-контракта, но UI их не предлагает —
 *  иначе на экране было бы два способа задать один и тот же период, и они
 *  разъезжались: пресет считает диапазон на сервере, поля показывают
 *  `period.from`/`period.to` из ответа. */
export function buildAiSpendPeriodQuery(fromDate: string, toDate: string): URLSearchParams {
  const query = new URLSearchParams({ preset: 'custom' })
  if (fromDate && toDate) {
    query.set('from', fromDate)
    query.set('to', toDate)
  }
  return query
}

export const SPEND_CHART_METRICS: { id: SpendChartMetric; label: string }[] = [
  { id: 'cost', label: 'Расходы' },
  { id: 'calls', label: 'Вызовы' },
  { id: 'tokens', label: 'Токены' },
]

export function buildAiSpendEventsQuery(options: {
  fromDate: string
  toDate: string
  q?: string
  kindGroup?: string
  status?: string
  attention?: string
  page?: number
  pageSize?: number
}): URLSearchParams {
  const query = buildAiSpendPeriodQuery(options.fromDate, options.toDate)
  if (options.q?.trim()) query.set('q', options.q.trim())
  if (options.kindGroup && options.kindGroup !== 'all') query.set('kind_group', options.kindGroup)
  if (options.status) query.set('status', options.status)
  if (options.attention) query.set('attention', options.attention)
  if (options.page && options.page > 1) query.set('page', String(options.page))
  if (options.pageSize) query.set('page_size', String(options.pageSize))
  return query
}

/** Значения по умолчанию: всё и расходы. */
export const SPEND_DEFAULT_KIND: SpendKindFilter = 'all'
export const SPEND_DEFAULT_METRIC: SpendChartMetric = 'cost'

/**
 * Типы операций для переключателей: «Все» плюс все группы из ответа сервера.
 *
 * Список нужен в двух местах — десктопной группе плашек и мобильной панели,
 * поэтому собирается один раз. Разъехавшиеся наборы означали бы, что
 * мобильный предлагает группу, которой нет в десктопном списке.
 */
export function spendFilterKindOptions(groups: AiSpendBreakdownRow[]): { id: SpendKindFilter; label: string }[] {
  return [
    { id: SPEND_DEFAULT_KIND, label: 'Все' },
    ...groups.map((item) => ({ id: item.id as SpendKindFilter, label: item.label })),
  ]
}

/**
 * Подпись кнопки: всегда выбранное значение.
 *
 * Раньше при стандартном выборе кнопка писала «Фильтры» — но одиннадцать
 * плашек показывали выбор сразу, и молчащая кнопка теряла это. Теперь на
 * старте читается «Все · Расходы», и дальше подпись следует за выбором:
 * писать «Все · Расходы» и после выбора «Скрипты» значило бы, что кнопка
 * врёт, а на экране совсем другое.
 */
export function spendFilterButtonLabel(
  kindLabel: string,
  kindFilter: SpendKindFilter,
  metric: SpendChartMetric,
): string {
  const metricName = SPEND_CHART_METRICS.find((item) => item.id === metric)?.label || metric
  // «Все» берётся из списка типов операций, а не пишется здесь константой:
  // подпись на кнопке и первая строка панели обязаны совпадать, иначе они
  // назовут один и тот же выбор разными словами.
  const kindName = kindFilter === SPEND_DEFAULT_KIND ? (kindLabel || 'Все') : kindLabel
  return `${kindName} · ${metricName}`
}

export function spendChartSeries(
  analytics: AiSpendAnalytics | null,
  kindFilter: SpendKindFilter,
): AiSpendDailyPoint[] {  if (!analytics) return []
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
/** Отступ по вертикали: та же величина, что и `padY` в компоненте графика. */
export const SPEND_CHART_PAD_Y = 4
/** Горизонтальных делений на всю высоту, вместе с осью у низа. */
export const SPEND_CHART_GRID_ROWS = 5

/**
 * Координаты линий сетки в системе `viewBox` графика.
 *
 * Вертикали повторяют подписанные дни: `spendChartLabelIndexes` уже считает
 * шаг так, чтобы подписи не наезжали друг на друга, и линия сетки встаёт
 * ровно под подписью. Считать отдельно «сколько влезет» означало бы
 * разрешить графику и подписям разойтись: подпись окажется без линии.
 *
 * Горизонтали равномерны и идут от нуля к максимуму периода. Их число
 * фиксировано, а не выводится из диапазона значений: прижатая к потолку
 * линия читалась бы как «максимум», а на пустых днях дала бы деления
 * «0,00 ₽» с шагом в копейки, которых на графике нет.
 */
export function spendChartGrid(
  pointCount: number,
  height = SPEND_CHART_HEIGHT,
  padY = SPEND_CHART_PAD_Y,
  width = SPEND_CHART_WIDTH,
  padX = SPEND_CHART_PAD_X,
  verticalIndexes = spendChartLabelIndexes(pointCount),
  horizontalRows = SPEND_CHART_GRID_ROWS,
): { verticals: number[]; horizontals: number[] } {
  const innerH = Math.max(height - padY * 2, 1)
  const innerW = Math.max(width - padX * 2, 1)
  const horizontals: number[] = []
  for (let row = 1; row < horizontalRows; row += 1) {
    horizontals.push(padY + (innerH / horizontalRows) * row)
  }
  const verticals = verticalIndexes.map((index) => (
    pointCount <= 1 ? padX + innerW / 2 : padX + (index / Math.max(pointCount - 1, 1)) * innerW
  ))
  return { verticals, horizontals }
}

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

/**
 * Горизонтальная позиция подписи дня в процентах от левого края графика.
 *
 * Подписи лежат в отдельном слое под SVG, в процентах — поэтому не зависят от
 * растяжения графика и считаются в любой ширине. Позиция повторяет
 * `spendChartIndexFromSvgX` наоборот: та же самая `padX`-вставка, что и у точек
 * графика, поэтому подпись встаёт ровно под своей точкой, а не «примерно там».
 * Каждая подпись сама центрируется над точкой и первая/последняя прижимается к
 * краю, чтобы текст не вылезал за границы карточки. При 7 днях ничего не
 * меняется; при длинном периоде уходит именно тот флейк «дни улетают вправо».
 */
export function spendChartLabelPosition(
  index: number,
  total: number,
  width = SPEND_CHART_WIDTH,
  padX = SPEND_CHART_PAD_X,
): number {
  if (total <= 1) return ((padX + (width - padX * 2) / 2) / width) * 100
  const innerW = Math.max(width - padX * 2, 1)
  const ratio = Math.max(0, Math.min(1, index / (total - 1)))
  return ((padX + ratio * innerW) / width) * 100
}

/**
 * Шаг между подписанными днями: между двумя видимыми подписями должно
 * оставаться не меньше `minLabelPx`, иначе они наезжают друг на друга.
 *
 * Ширина слоя подписей совпадает с шириной SVG, а тот растягивается
 * `preserveAspectRatio="none"` на всю карточку. Реальная ширина на этапе
 * расчёта неизвестна, поэтому берётся консервативная — 320px: на телефоне
 * слой уже этого, а на десктопе подписи просто выглядят реже.
 */
export const SPEND_CHART_LABEL_REFERENCE_PX = 320

/** Подпись «дд.мм» в 11px занимает около 30px — столько нужно между датами. */
export const SPEND_CHART_LABEL_MIN_LABEL_PX = 30

/** Ширина одного дня в пикселях слоя подписей при опорной ширине. */
function spendChartLabelDayPx(pointCount: number, referenceWidthPx: number): number {
  const plotRatio = (referenceWidthPx / SPEND_CHART_WIDTH) * (SPEND_CHART_WIDTH - SPEND_CHART_PAD_X * 2)
  return plotRatio / Math.max(pointCount - 1, 1)
}

/**
 * При `viewBox 800` и слое шириной 320px один день занимает
 * `320 / (pointCount - 1)` пикселей, а подпись «дд.мм» в 11px — около 30px.
 * Между двумя видимыми подписями проходит `step - 1` дней, поэтому шаг
 * обязан довести именно этот зазор до `minLabelPx`.
 */
export function spendChartLabelStep(
  pointCount: number,
  minLabelPx = SPEND_CHART_LABEL_MIN_LABEL_PX,
  referenceWidthPx = SPEND_CHART_LABEL_REFERENCE_PX,
): number {
  if (pointCount <= 1) return 1
  const perPointPx = spendChartLabelDayPx(pointCount, referenceWidthPx)
  if (perPointPx >= minLabelPx) return 1
  return Math.max(1, Math.ceil(minLabelPx / perPointPx) + 1)
}

/**
 * Индексы дней, у которых остаётся видимая подпись.
 *
 * Сетка идёт от последнего дня назад: так шаг одинаков по всему периоду,
 * включая хвост. Раньше последний день добавлялся «сверх» сетки, и на длинном
 * периоде он прилипал к соседу вплотную (на 365 днях — зазор 4px вместо 30),
 * обе подписи сливались в «04.0104.01».
 *
 * Первая дата — начало периода, и она нужна читателю сильнее всего, поэтому
 * добавляется всегда. Но если для неё не хватает места (сосед уже в паре
 * десятков пикселей), сосед выбрасывается: две слипшиеся подписи хуже, чем
 * одна, а «первый день» всё равно виден при выборе периода и в тултипе.
 */
export function spendChartLabelIndexes(
  pointCount: number,
  step = spendChartLabelStep(pointCount),
  minLabelPx = SPEND_CHART_LABEL_MIN_LABEL_PX,
  referenceWidthPx = SPEND_CHART_LABEL_REFERENCE_PX,
): number[] {
  if (pointCount <= 0) return []
  if (pointCount === 1) return [0]
  const indexes: number[] = []
  for (let index = pointCount - 1; index >= 0; index -= step) indexes.push(index)
  indexes.reverse()
  if (indexes[0] === 0) return indexes
  // Места под первую дату может не хватить: тогда её ближайший сосед
  // выбрасывается. Одна подпись лучше, чем две слипшиеся в «01.0202.01».
  const dayPx = spendChartLabelDayPx(pointCount, referenceWidthPx)
  if (indexes.length > 1 && indexes[0] * dayPx < minLabelPx) indexes.shift()
  indexes.unshift(0)
  return indexes
}

/**
 * Позиция активной точки в процентах от левого верхнего угла области построения.
 *
 * Точка рисуется HTML-элементом поверх SVG, потому что `viewBox` с
 * `preserveAspectRatio="none"` сплющивает круг в овал. Но проценты берутся от
 * той же системы координат, что и сами точки графика, — иначе кольцо уехало бы
 * с вершины на длинных периодах, где ошибка накапливается по всему `viewBox`.
 */
export function spendChartDotStyle(
  x: number,
  y: number,
  width = SPEND_CHART_WIDTH,
  height = SPEND_CHART_HEIGHT,
): { left: string; top: string } {
  return {
    left: `${(x / width) * 100}%`,
    top: `${(y / height) * 100}%`,
  }
}

export function spendShiftIsoDate(iso: string, days: number): string {  const [year, month, day] = iso.split('-').map(Number)
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
