import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import {
  fetchAiSpendAnalytics,
  fetchAiSpendDay,
  fetchAiSpendEvents,
  fetchAiSpendSummary,
  type AiSpendAnalytics,
  type AiSpendAttentionItem,
  type AiSpendBreakdownRow,
  type AiSpendDailyPoint,
  type AiSpendDay,
  type AiSpendEvent,
  type AiSpendEventsPage,
  type AiSpendSummary,
  type AiSpendTopEntity,
} from './api'
import { isoToShortDate, moscowDateInputValue, moscowMonthStart, shortDateToIso } from './dateTime'
import { lockBodyScroll } from './bodyScrollLock'
import { SpendFilterSheet } from './SpendFilterSheet'
import {
  SPEND_CHART_HEIGHT,
  SPEND_CHART_METRICS,
  SPEND_CHART_PAD_X,
  SPEND_CHART_PAD_Y,
  SPEND_CHART_WIDTH,
  buildAiSpendEventsQuery,
  buildAiSpendPeriodQuery,
  formatSpendDelta,
  shareWidth,
  spendChartDotStyle,
  spendChartGrid,
  spendChartIndexFromSvgX,
  spendChartLabelIndexes,
  spendChartLabelPosition,
  spendChartSeries,
  spendChartValue,
  spendDayTitle,
  spendFilterButtonLabel,
  spendFilterKindOptions,
  type SpendChartMetric,
  type SpendKindFilter,
} from './aiSpendView'

function formatToken(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('ru-RU').format(value)
}

function formatUsd(value: number | null | undefined) {
  if (value == null) return 'оценка недоступна'
  return `$${value.toFixed(4)}`
}

function formatPercent(value: number | null | undefined) {
  if (value == null) return '—'
  return `${String(value).replace('.', ',')}%`
}

function Delta({ value, compact }: { value: number | null | undefined; compact?: boolean }) {
  const delta = formatSpendDelta(value)
  if (!delta) return null
  return <em className={`ai-spend-delta ${delta.tone}`}>{delta.text}{compact ? null : <> <span>к предыдущему периоду</span></>}</em>
}

function pointerToSvgX(svg: SVGSVGElement, clientX: number): number | null {
  const matrix = svg.getScreenCTM()
  if (!matrix) return null
  const point = svg.createSVGPoint()
  point.x = clientX
  point.y = 0
  return point.matrixTransform(matrix.inverse()).x
}

function SpendChart({
  series,
  metric,
}: {
  series: AiSpendDailyPoint[]
  metric: SpendChartMetric
}) {
  const [hover, setHover] = useState<number | null>(null)
  // На телефоне у SVG нет мыши: `hover` никогда не меняется, и график
  // навсегда замирает на последнем дне. `scrubIndex` — та же точка,
  // выбранная пальцем или стрелками клавиатуры, а `hover` остаётся
  // для мыши. Активной считается мышиная точка, если она есть.
  const [scrubIndex, setScrubIndex] = useState<number | null>(null)
  const values = series.map((point) => spendChartValue(point, metric))
  const max = Math.max(...values, 0)
  const width = SPEND_CHART_WIDTH
  const height = SPEND_CHART_HEIGHT
  const padX = SPEND_CHART_PAD_X
  // Отступ по вертикали берётся из общей константы, а не пишется здесь
  // числом: сетка строится по нему же, и две копии величины разъехались бы
  // на доли единицы — линии встали бы не туда.
  const padY = SPEND_CHART_PAD_Y
  const innerW = width - padX * 2
  const innerH = height - padY * 2
  const points = series.map((point, index) => {
    const x = series.length === 1 ? padX + innerW / 2 : padX + (index / Math.max(series.length - 1, 1)) * innerW
    const ratio = max > 0 ? spendChartValue(point, metric) / max : 0
    const y = padY + innerH - ratio * innerH
    return { x, y, point }
  })
  const line = points.map((item) => `${item.x},${item.y}`).join(' ')
  const area = points.length
    ? `${padX},${padY + innerH} ${line} ${padX + innerW},${padY + innerH}`
    : ''
  const activeIndex = hover ?? scrubIndex ?? (points.length ? points.length - 1 : 0)
  const active = points[activeIndex]
  const labelledIndexes = new Set(spendChartLabelIndexes(series.length))
  const grid = spendChartGrid(series.length)

  function updateHover(event: { currentTarget: SVGSVGElement; clientX: number }) {
    const svgX = pointerToSvgX(event.currentTarget, event.clientX)
    if (svgX == null) return
    setHover(spendChartIndexFromSvgX(svgX, series.length, width, padX))
  }

  function scrubTo(event: { currentTarget: SVGSVGElement; clientX: number }) {
    const svgX = pointerToSvgX(event.currentTarget, event.clientX)
    if (svgX == null) return
    setScrubIndex(spendChartIndexFromSvgX(svgX, series.length, width, padX))
  }

  // Стрелки двигают ту же точку, что и палец, — иначе график недоступен
  // с клавиатуры: `onMouseMove` туда не попадает.
  function moveScrub(step: number) {
    if (!points.length) return
    setScrubIndex((current) => {
      const from = current ?? points.length - 1
      return Math.max(0, Math.min(points.length - 1, from + step))
    })
  }

  const scrubLabel = active
    ? `${active.point.label}: ${active.point.estimated_cost_rub_label}, ${active.point.paid_calls_label}, ${active.point.total_tokens_label} токенов`
    : 'Нет данных за период'

  return <div className="ai-spend-chart-canvas">
    {/* Тултип стоит над графиком, а не под ним: под осью он занимал отдельную
        строку, и график прыгал вверх-вниз при смене выбранного дня.
        Значения идут в потоке, а не отдельными боксами: на телефоне четыре
        бокса переносились на четыре строки, а здесь строка переносится
        как текст — заголовок плюс одна строка цифр. */}
    {active ? <div className="ai-spend-chart-tooltip">
      <strong>{active.point.label}</strong>
      <span>{active.point.estimated_cost_rub_label}</span>
      <span aria-hidden="true">·</span>
      <span>{active.point.paid_calls_label}</span>
      <span aria-hidden="true">·</span>
      <span>{active.point.total_tokens_label} токенов</span>
      {active.point.unknown_cost_calls ? <span className="ai-spend-chart-tooltip-note">есть вызовы без оценки</span> : null}
    </div> : null}
    {/* Точка вынесена из SVG в HTML: `viewBox` с `preserveAspectRatio="none"`
        растягивает оси неравномерно, и любой круг внутри сплющивается в овал —
        тем сильнее, чем уже окно. Написать радиус в пикселях (`r="4px"`) не
        помогает: `px` в атрибуте SVG значит единицу локальной системы, а она
        потом растягивается тем же неравномерным преобразованием. Слой с точками
        находится над `viewBox`, поэтому круг остаётся круглым на любой ширине. */}
    <div className="ai-spend-chart-plot">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label="Динамика расходов"
        onMouseLeave={() => setHover(null)}
        onMouseMove={updateHover}
        onPointerDown={scrubTo}
        onPointerMove={(event) => { if (event.pointerType !== 'mouse') scrubTo(event) }}
      >
        {/* Сетка рисуется до заливки, а не после. Иначе пунктир оказывается
            поверх голубой подложки и читается как штриховка области, а не как
            координатная сетка. Вертикали совпадают с подписями дней. */}
        <g className="ai-spend-chart-grid">
          {grid.verticals.map((x) => <line key={`v${x}`} x1={x} y1={padY} x2={x} y2={padY + innerH} />)}
          {grid.horizontals.map((y) => <line key={`h${y}`} x1={padX} y1={y} x2={padX + innerW} y2={y} />)}
        </g>
        <line className="ai-spend-chart-axis" x1={padX} y1={padY + innerH} x2={padX + innerW} y2={padY + innerH} />
        {area ? <polygon className="ai-spend-chart-area" points={area} /> : null}
        {line ? <polyline className="ai-spend-chart-line" fill="none" points={line} /> : null}
      </svg>
      {active ? <span
        className="ai-spend-chart-dot active"
        aria-hidden="true"
        style={spendChartDotStyle(active.x, active.y)}
      /> : null}
    </div>
    <div className="ai-spend-chart-labels" aria-hidden="true">
      {series.map((point, index) => {
        const last = series.length - 1
        // Сетка подписей идёт от последнего дня назад (см. `spendChartLabelIndexes`),
        // поэтому крайние даты не липнут друг к другу на длинном периоде.
        const visible = labelledIndexes.has(index)
        const atStart = index === 0
        const atEnd = index === last
        return <span
          key={point.date}
          className={visible ? '' : 'hidden'}
          style={{
            left: `${spendChartLabelPosition(index, series.length)}%`,
            transform: `translateX(${atStart ? '0' : atEnd ? '-100%' : '-50%'})`,
          }}
        >{point.short_label}</span>
      })}
    </div>
    {/* График читается пальцем и стрелками: ползунок подсказывает, что
        значение меняется, и получает фокус с клавиатуры. */}
    <input
      className="ai-spend-chart-scrub"
      type="range"
      min={0}
      max={Math.max(points.length - 1, 0)}
      step={1}
      value={activeIndex}
      onChange={(event) => {
        setScrubIndex(Number(event.target.value))
        setHover(null)
      }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') { moveScrub(-1); event.preventDefault() }
        if (event.key === 'ArrowRight') { moveScrub(1); event.preventDefault() }
      }}
      aria-label={`День на графике. ${scrubLabel}`}
      aria-valuetext={scrubLabel}
    />
  </div>
}

function BreakdownList({
  rows,
  empty,
  onSelect,
}: {
  rows: AiSpendBreakdownRow[]
  empty: string
  onSelect?: (id: string) => void
}) {
  const visible = rows.filter((row) => row.paid_calls > 0 || (row.estimated_cost_rub ?? 0) > 0)
  if (!visible.length) return <p className="ai-spend-empty">{empty}</p>
  return <ul className="ai-spend-bars">
    {visible.map((row) => (
      <li key={row.id}>
        <button type="button" onClick={onSelect ? () => onSelect(row.id) : undefined} disabled={!onSelect}>
          <div>
            <strong>{row.label}</strong>
            <span>{row.calls_label}{row.share == null ? '' : ` · ${formatPercent(row.share)}`}</span>
          </div>
          <b>{row.estimated_cost_rub_label}</b>
        </button>
        <span className="ai-spend-bar-track"><i style={{ width: shareWidth(row.share) }} /></span>
      </li>
    ))}
  </ul>
}

function EventDetails({ event }: { event: AiSpendEvent }) {
  const model = event.model_label || event.model
  return <dl className="ai-spend-tech">
    <div><dt>Модель</dt><dd>{model || '—'}</dd></div>
    <div><dt>Тип вызова</dt><dd>{event.kind_label}{event.kind && event.kind !== event.kind_label ? ` · ${event.kind}` : ''}</dd></div>
    <div><dt>Input tokens</dt><dd>{formatToken(event.input_tokens)}</dd></div>
    <div><dt>Cached input</dt><dd>{formatToken(event.cached_input_tokens)}</dd></div>
    <div><dt>Cache hit %</dt><dd>{formatPercent(event.cache_hit_percent)}</dd></div>
    <div><dt>Cache write</dt><dd>{formatToken(event.cache_write_tokens)}</dd></div>
    <div><dt>Output tokens</dt><dd>{formatToken(event.output_tokens)}</dd></div>
    {event.reasoning_tokens != null ? <div><dt>Reasoning tokens</dt><dd>{formatToken(event.reasoning_tokens)}</dd></div> : null}
    {event.duration_seconds != null ? <div><dt>Длительность</dt><dd>{formatToken(Math.round(event.duration_seconds))} сек</dd></div> : null}
    <div><dt>Attempt</dt><dd>{event.attempt == null ? '—' : event.attempt}</dd></div>
    <div><dt>Status</dt><dd>{event.status_label || event.status || '—'}</dd></div>
    {event.run_id ? <div><dt>run_id</dt><dd>{event.run_id}</dd></div> : null}
    {event.job_id ? <div><dt>job_id</dt><dd>{event.job_id}</dd></div> : null}
    <div><dt>Стоимость USD</dt><dd>{formatUsd(event.estimated_cost_usd)}</dd></div>
    <div><dt>Стоимость RUB</dt><dd>{event.estimated_cost_rub_label}</dd></div>
  </dl>
}

function DayList({
  series,
  today,
  onOpenDay,
}: {
  series: AiSpendDailyPoint[]
  today: string
  onOpenDay: (point: AiSpendDailyPoint) => void
}) {
  const days = [...series].reverse()
  if (!days.length) return <p className="ai-spend-empty">За период дней с данными нет.</p>
  return <>
    <ul className="ai-spend-day-list">
      {days.map((point) => (
        <li key={point.date}>
          <button type="button" onClick={() => onOpenDay(point)}>
            <span>
              <strong>{spendDayTitle(point.date, today, point.label)}</strong>
              <small>{point.paid_calls_label}</small>
            </span>
            <b>{point.estimated_cost_rub_label}</b>
          </button>
        </li>
      ))}
    </ul>
  </>
}

/** Одна строка вызова: журнал и дневное окно показывают одни и те же
 *  шесть значений, поэтому разметка живёт здесь, а не дублируется.
 *  `compact` раскладывает строку в подписанную карточку на узком экране. */
function EventRow({
  event,
  open,
  compact,
  onToggle,
}: {
  event: AiSpendEvent
  open: boolean
  compact?: boolean
  onToggle: () => void
}) {
  return <button
    type="button"
    className={compact ? 'ai-spend-row compact' : 'ai-spend-row'}
    onClick={onToggle}
    aria-expanded={open}
  >
    <span><i className="ai-spend-cell-label">Время</i>{event.datetime_label || event.time}</span>
    <span><i className="ai-spend-cell-label">Сущность</i>{event.entity_label || 'Без сущности'}</span>
    <span><i className="ai-spend-cell-label">Операция</i>{event.kind_label}</span>
    <span><i className="ai-spend-cell-label">Модель</i>{event.model_label || event.model || '—'}</span>
    <strong><i className="ai-spend-cell-label">Стоимость</i>{event.estimated_cost_rub_label}</strong>
    <em className={event.status === 'error' ? 'error' : 'ok'}><i className="ai-spend-cell-label">Статус</i>{event.status_label || event.status || '—'}</em>
  </button>
}

function DayJournalModal({
  point,
  today,
  onClose,
}: {
  point: AiSpendDailyPoint
  today: string
  onClose: () => void
}) {
  const [day, setDay] = useState<AiSpendDay | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [openKey, setOpenKey] = useState('')
  const dialogRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setDay(null)
    void fetchAiSpendDay(point.date)
      .then((payload) => { if (!cancelled) setDay(payload) })
      .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [point.date])

  useEffect(() => lockBodyScroll(), [])

  // `aria-modal="true"` — обещание, которое DOM сам не держит: без ловушки
  // Tab уходит на 30 строк журнала под окном, а фокус в окно не приходит.
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const previous = document.activeElement as HTMLElement | null
    dialog.focus()
    return () => { previous?.focus?.() }
  }, [])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') { onClose(); return }
      if (event.key !== 'Tab') return
      const dialog = dialogRef.current
      if (!dialog) return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ))
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        last.focus()
        event.preventDefault()
      } else if (!event.shiftKey && active === last) {
        first.focus()
        event.preventDefault()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return <div className="ai-spend-modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section
      className="ai-spend-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ai-spend-day-title"
      ref={dialogRef}
      tabIndex={-1}
    >
      <header>
        <div>
          <h2 id="ai-spend-day-title">{spendDayTitle(point.date, today, point.label)}</h2>
          <p>{point.estimated_cost_rub_label} · {point.paid_calls_label}</p>
        </div>
        <button type="button" className="ai-spend-modal-close" onClick={onClose} aria-label="Закрыть">×</button>
      </header>
      <div className="ai-spend-modal-body">
        {loading ? <p className="ai-spend-empty">Загружаем день…</p> : null}
        {error ? <p className="dc-alert error">{error}</p> : null}
        {day && !day.events.length && !loading ? <p className="ai-spend-empty">За этот день платных вызовов нет.</p> : null}
        {day?.events.map((event, index) => {
          const key = event.event_key || `${event.at}-${event.kind}-${event.entity_id}-${index}`
          const open = openKey === key
          return <article key={key} className="ai-spend-day-event">
            <EventRow event={event} open={open} compact onToggle={() => setOpenKey(open ? '' : key)} />
            {open ? <EventDetails event={event} /> : null}
          </article>
        })}
      </div>
    </section>
  </div>
}

export function AiSpendDashboardCard({ onOpen }: { onOpen: () => void }) {
  const [summary, setSummary] = useState<AiSpendSummary | null>(null)
  useEffect(() => {
    void fetchAiSpendSummary().then(setSummary).catch(() => undefined)
  }, [])
  return <button type="button" className="ai-spend-teaser" onClick={onOpen}>
    <span>₽</span>
    <div>
      <small>AI сегодня</small>
      <strong>{summary?.today.estimated_cost_rub_label || '…'}</strong>
    </div>
  </button>
}

/**
 * Поднимает календарь ОС для невидимного нативного входа.
 *
 * `showPicker()` разрешён только из пользовательского жеста, поэтому он
 * вызывается прямо в обработчике клика, а не после setState. Firefox
 * и старые WebKit его не дают — там пользователь открывает календарь,
 * тапнув по самому полю.
 */
function openNativePicker(picker: RefObject<HTMLInputElement | null>) {
  try {
    picker.current?.showPicker()
  } catch {
    picker.current?.focus()
  }
}

export function AiSpend() {
  const today = moscowDateInputValue()
  // Относительных пресетов нет: период всегда задан датами, по умолчанию —
  // с первого числа текущего месяца по сегодня. Считать это на клиенте
  // можно было бы, но период принадлежит серверу: он же строит график и
  // суммы по тем же правилам, а вторая копия правила рано или поздно
  // разъезжается с первой.
  const [fromText, setFromText] = useState(() => isoToShortDate(moscowMonthStart(today)))
  const [toText, setToText] = useState(() => isoToShortDate(today))
  const fromIso = shortDateToIso(fromText)
  const toIso = shortDateToIso(toText)
  const datesInvalid = !fromIso || !toIso || fromIso > toIso
  const fromPickerRef = useRef<HTMLInputElement>(null)
  const toPickerRef = useRef<HTMLInputElement>(null)
  const [metric, setMetric] = useState<SpendChartMetric>('cost')
  const [kindFilter, setKindFilter] = useState<SpendKindFilter>('all')
  // Панель фильтров живёт только на мобильном; на десктопе обе группы
  // переключателей видны сразу и открывать нечего.
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [attentionFilter, setAttentionFilter] = useState('')
  const [kindGroupFilter, setKindGroupFilter] = useState('')
  const [page, setPage] = useState(1)
  const [openKey, setOpenKey] = useState('')
  const [openDay, setOpenDay] = useState<AiSpendDailyPoint | null>(null)
  const [analytics, setAnalytics] = useState<AiSpendAnalytics | null>(null)
  const [events, setEvents] = useState<AiSpendEventsPage | null>(null)
  const [error, setError] = useState('')
  const [eventsError, setEventsError] = useState('')
  const [loading, setLoading] = useState(true)
  const journalRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 300)
    return () => window.clearTimeout(timer)
  }, [search])

  // Пока строка даты не разобралась, запрос уходить не должен: сервер на
  // пустые `from`/`to` молча отдал бы дефолтные 30 дней, и на экране
  // «31.02.2026» выглядело бы как «период принят».
  const datesPending = datesInvalid
  const periodQuery = useMemo(
    () => buildAiSpendPeriodQuery(fromIso ?? '', toIso ?? ''),
    [fromIso, toIso],
  )
  const eventsQuery = useMemo(
    () => buildAiSpendEventsQuery({
      fromDate: fromIso ?? '',
      toDate: toIso ?? '',
      q: debouncedSearch,
      kindGroup: kindGroupFilter,
      status: statusFilter,
      attention: attentionFilter,
      page,
      pageSize: 20,
    }),
    [fromIso, toIso, debouncedSearch, kindGroupFilter, statusFilter, attentionFilter, page],
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    if (datesPending) {
      setAnalytics(null)
      setLoading(false)
      return () => { cancelled = true }
    }
    void fetchAiSpendAnalytics(periodQuery)
      .then((payload) => {
        if (cancelled) return
        setAnalytics(payload)
        setKindFilter('all')
      })
      .catch((reason) => {
        if (cancelled) return
        setAnalytics(null)
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [periodQuery, datesPending])

  useEffect(() => {
    let cancelled = false
    setEventsError('')
    if (datesPending) {
      setEvents(null)
      return () => { cancelled = true }
    }
    void fetchAiSpendEvents(eventsQuery)
      .then((payload) => { if (!cancelled) setEvents(payload) })
      .catch((reason) => {
        if (cancelled) return
        setEvents(null)
        setEventsError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { cancelled = true }
  }, [eventsQuery, datesPending])

  useEffect(() => { setPage(1) }, [fromIso, toIso, debouncedSearch, kindGroupFilter, statusFilter, attentionFilter])
  // `kindFilter` раньше закрывал открытый дневной модал: смена вида
  // графика выбрасывала окно, которое читатель только что открыл.
  useEffect(() => { setOpenDay(null) }, [fromIso, toIso])

  const series = spendChartSeries(analytics, kindFilter)
  // Поля показывают период, который сервер реально применил, — тот же, по
  // которому построены график и суммы. Иначе при обрезанном сервером `to`
  // (выбранная дата в будущем) поле обещало бы больше, чем показано.
  const shownFromIso = analytics?.period.from || fromIso || ''
  const shownToIso = analytics?.period.to || toIso || ''
  const shownFrom = shownFromIso ? isoToShortDate(shownFromIso) : fromText
  const shownTo = shownToIso ? isoToShortDate(shownToIso) : toText
  const hasChart = series.some((point) => spendChartValue(point, metric) > 0 || point.unknown_cost_calls > 0)
  const kindOptions = spendFilterKindOptions(analytics?.kind_groups || [])
  const filterButtonLabel = spendFilterButtonLabel(
    kindOptions.find((item) => item.id === kindFilter)?.label || '',
    kindFilter,
    metric,
  )
  const totals = analytics?.totals
  const filtersActive = Boolean(debouncedSearch || kindGroupFilter || statusFilter || attentionFilter)

  function openJournal(patch: { attention?: string; kindGroup?: string; search?: string }) {
    if (patch.attention !== undefined) setAttentionFilter(patch.attention)
    if (patch.kindGroup !== undefined) setKindGroupFilter(patch.kindGroup)
    if (patch.search !== undefined) {
      setSearch(patch.search)
      setDebouncedSearch(patch.search)
    }
    window.setTimeout(() => journalRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }

  // Подписи плашек берём из тех же списков, что и контролы, чтобы
  // «Транскрибация» на карточке и в плашке назывались одинаково.
  const kindGroupLabel = analytics?.kind_groups.find((item) => item.id === kindGroupFilter)?.label
  const attentionLabel = analytics?.attention.find((item) => item.id === attentionFilter)?.title
  const activeFilterChips = [
    attentionFilter ? { id: 'attention', label: `Сигнал: ${attentionLabel || attentionFilter}`, onClear: () => setAttentionFilter('') } : null,
    kindGroupFilter ? { id: 'kind', label: `Операции: ${kindGroupLabel || kindGroupFilter}`, onClear: () => setKindGroupFilter('') } : null,
    statusFilter ? { id: 'status', label: `Статус: ${statusFilter === 'error' ? 'ошибка' : 'успешно'}`, onClear: () => setStatusFilter('') } : null,
    debouncedSearch ? { id: 'search', label: `Поиск: ${debouncedSearch}`, onClear: () => { setSearch(''); setDebouncedSearch('') } } : null,
  ].filter((chip): chip is { id: string; label: string; onClear: () => void } => chip !== null)

  return <div className="ai-spend-page">
    <header className="dc-header ai-spend-header">
      <div className="dc-header-title">
        <h1>Расходы AI</h1>
      </div>
      {analytics && totals ? <section className="ai-spend-kpis" aria-label="Ключевые показатели">
        <article>
          <small>Расход за сегодня</small>
          <strong>{analytics.today?.estimated_cost_rub_label || '…'}</strong>
        </article>
        <article>
          <small>Расход за вчера</small>
          <strong>{analytics.yesterday?.estimated_cost_rub_label || '…'}</strong>
        </article>
        <article>
          <small>Расход за период</small>
          <strong>{totals.estimated_cost_rub_label}</strong>
          <Delta value={analytics.comparison.cost_percent} compact />
        </article>
        <article>
          <small>Платных вызовов</small>
          <strong>{formatToken(totals.paid_calls)}</strong>
          <Delta value={analytics.comparison.calls_percent} compact />
        </article>
        <article>
          <small>Средний запрос</small>
          <strong>{totals.average_cost_rub_label}</strong>
          <Delta value={analytics.comparison.average_cost_percent} compact />
        </article>
        <article>
          <small>Токенов всего</small>
          <strong>{totals.total_tokens_label}</strong>
          <Delta value={analytics.comparison.tokens_percent} compact />
        </article>
      </section> : null}
      <div className="ai-spend-period">
        <div className="ai-spend-custom">
          {/* Относительных пресетов больше нет: единственный вход в период —
              эти два поля, по умолчанию с первого числа месяца по сегодня. */}
          <span className="ai-spend-date">
            <input
              type="text"
              inputMode="numeric"
              autoComplete="off"
              aria-label="С какой даты"
              placeholder="дд.мм.гггг"
              value={shownFrom}
              maxLength={10}
              aria-invalid={datesInvalid}
              aria-describedby={datesInvalid ? 'ai-spend-date-error' : undefined}
              onChange={(event) => setFromText(event.target.value)}
              onClick={() => openNativePicker(fromPickerRef)}
            />
            {/* Невидимный нативный вход: поднимает календарь ОС и хранит ISO,
                а пользователь продолжает видеть `дд.мм.гггг`.
                Только `max={today}`: с перекрёстными `min`/`max` календарь
                сужался до единственной доступной даты и переставал
                переключаться — обратный порядок честно показывает ошибка
                под полями, а не блокировкой выбора. */}
            <input
              ref={fromPickerRef}
              type="date"
              className="ai-spend-date-overlay"
              tabIndex={-1}
              aria-hidden="true"
              value={shownFromIso}
              max={today}
              onChange={(event) => {
                if (!event.target.value) return
                setFromText(isoToShortDate(event.target.value))
              }}
            />
          </span>
          <span className="ai-spend-date">
            <input
              type="text"
              inputMode="numeric"
              autoComplete="off"
              aria-label="По какую дату"
              placeholder="дд.мм.гггг"
              value={shownTo}
              maxLength={10}
              aria-invalid={datesInvalid}
              aria-describedby={datesInvalid ? 'ai-spend-date-error' : undefined}
              onChange={(event) => setToText(event.target.value)}
              onClick={() => openNativePicker(toPickerRef)}
            />
            <input
              ref={toPickerRef}
              type="date"
              className="ai-spend-date-overlay"
              tabIndex={-1}
              aria-hidden="true"
              value={shownToIso}
              max={today}
              onChange={(event) => {
                if (!event.target.value) return
                setToText(isoToShortDate(event.target.value))
              }}
            />
          </span>
        </div>
        {datesInvalid
          ? <small id="ai-spend-date-error" className="ai-spend-date-error">Период не выбран или перепутан: сначала более ранняя дата.</small>
          : null}
      </div>
    </header>

    {error ? <p className="dc-alert error">{error}</p> : null}
    {analytics?.skipped_lines ? <p className="ai-spend-note">Пропущены повреждённые строки дневника: {analytics.skipped_lines}.</p> : null}
    {loading && !analytics ? <p className="ai-spend-empty">Загружаем оценку расходов…</p> : null}

    {analytics && totals ? <>
      {totals.unknown_cost_calls ? <p className="ai-spend-note">Есть вызовы без оценки стоимости: {totals.unknown_cost_calls}. Они не входят в сумму.</p> : null}

      <section className="ai-spend-card ai-spend-chart">
        <header>
          <div className="ai-spend-chart-heading">
            <h2>Динамика расходов</h2>
            <div className="ai-spend-pills" role="group" aria-label="Какие операции показывать на графике">
              {kindOptions.map((item) => (
                <button key={item.id} type="button" className={kindFilter === item.id ? 'active' : ''} aria-pressed={kindFilter === item.id} onClick={() => setKindFilter(item.id)}>
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          {/* На телефоне плашки показателя скрыты, и справа от заголовка
              зияло пустое место. Кнопка занимает именно его, а не добавляет
              новую строку, поэтому высота карточки не меняется. */}
          <button
            type="button"
            className="ai-spend-filter-trigger"
            aria-haspopup="dialog"
            aria-expanded={filtersOpen}
            onClick={() => setFiltersOpen(true)}
          ><span aria-hidden="true">⚙</span> {filterButtonLabel}</button>
          <div className="ai-spend-pills compact" role="group" aria-label="Что показывать на графике">
            {SPEND_CHART_METRICS.map((item) => (
              <button key={item.id} type="button" className={metric === item.id ? 'active' : ''} aria-pressed={metric === item.id} onClick={() => setMetric(item.id)}>
                {item.label}
              </button>
            ))}
          </div>
        </header>
        <div className="ai-spend-chart-body">
          <DayList
            series={series}
            today={analytics.period.today}
            onOpenDay={setOpenDay}
          />
          {hasChart ? <SpendChart series={series} metric={metric} /> : <p className="ai-spend-empty">За выбранный период платных вызовов нет — график появится после первых записей.</p>}
        </div>
      </section>

      <div className="ai-spend-split">
        <section className="ai-spend-card">
          <header>
            <h2>Расходы по операциям</h2>
          </header>
          <BreakdownList
            rows={analytics.by_kind}
            empty="За период нет операций с оценкой стоимости."
            onSelect={(id) => openJournal({ kindGroup: kindGroupFromKind(analytics, id) })}
          />
        </section>
        <section className="ai-spend-card">
          <header>
            <h2>Расходы по моделям</h2>
          </header>
          <BreakdownList
            rows={analytics.by_model}
            empty="За период нет данных по моделям."
            onSelect={(id) => openJournal({ search: id === 'unknown' ? '' : id })}
          />
        </section>
      </div>

      <div className="ai-spend-split">
        <section className="ai-spend-card">
          <header>
            <h2>На что обратить внимание</h2>
            <p>Только проверяемые сигналы по дневнику</p>
          </header>
          {analytics.attention.length ? <ul className="ai-spend-attention">
            {analytics.attention.map((item: AiSpendAttentionItem) => (
              <li key={item.id} className={item.severity}>
                <div>
                  <strong>{item.title}</strong>
                  <span>{item.count_label}</span>
                  <p>{item.explanation}</p>
                </div>
                <button type="button" onClick={() => openJournal({ attention: item.id })}>Посмотреть</button>
              </li>
            ))}
          </ul> : <p className="ai-spend-empty">За период явных проблем не видно.</p>}
        </section>
        <section className="ai-spend-card">
          <header>
            <h2>Где потратили больше всего</h2>
          </header>
          {analytics.top_entities.length ? <ol className="ai-spend-entities">
            {analytics.top_entities.map((item: AiSpendTopEntity, index) => (
              <li key={`${item.entity_type || 'none'}-${item.entity_id || index}`}>
                <button type="button" onClick={() => openJournal({ search: item.entity_id || item.label })}>
                  <b>{index + 1}</b>
                  <div>
                    <strong>{item.label}</strong>
                    <span>{item.calls_label} · {item.primary_kind_label}</span>
                  </div>
                  <em>{item.estimated_cost_rub_label}</em>
                </button>
                {item.bitrix_url ? <a href={item.bitrix_url} target="_blank" rel="noreferrer">Bitrix</a> : null}
              </li>
            ))}
          </ol> : <p className="ai-spend-empty">За период нет сущностей с расходами.</p>}
        </section>
      </div>

      <section className="ai-spend-card ai-spend-journal" ref={journalRef}>
        <header>
          <div>
            <h2>Журнал вызовов</h2>
          </div>
          <span aria-live="polite">{events ? `${events.total} записей` : ''}</span>
        </header>
        <div className="ai-spend-journal-tools">
          <label className="ai-spend-field">
            <span>Поиск по сделке, лиду, модели или run_id</span>
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Сделка, лид, модель, run_id, job_id"
            />
          </label>
          <label className="ai-spend-field">
            <span>Тип операции</span>
            <select value={kindGroupFilter} onChange={(event) => setKindGroupFilter(event.target.value)}>
              <option value="">Все операции</option>
              {analytics.kind_groups.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </label>
          <label className="ai-spend-field">
            <span>Статус вызова</span>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
              <option value="">Все статусы</option>
              <option value="success">успешно</option>
              <option value="error">ошибка</option>
            </select>
          </label>
          {filtersActive ? <button type="button" className="ai-spend-clear" onClick={() => {
            setSearch('')
            setDebouncedSearch('')
            setKindGroupFilter('')
            setStatusFilter('')
            setAttentionFilter('')
          }}>Сбросить всё</button> : null}
        </div>
        {/* Каждый активный фильтр виден отдельной подписанной плашкой.
            Раньше только `attention` показывал, что журнал отфильтрован;
            переход из «Расходы по операциям» менял журнал молча, и
            нельзя было понять, применился фильтр или нет. */}
        {activeFilterChips.length ? <ul className="ai-spend-chips" aria-label="Действующие фильтры журнала">
          {activeFilterChips.map((chip) => <li key={chip.id}>
            <span>{chip.label}</span>
            <button type="button" onClick={chip.onClear} aria-label={`Убрать фильтр «${chip.label}»`}>×</button>
          </li>)}
        </ul> : null}
        {eventsError ? <p className="dc-alert error">{eventsError}</p> : null}
        {!events && !eventsError ? <p className="ai-spend-empty">Загружаем журнал…</p> : null}
        {events && !events.events.length ? <p className="ai-spend-empty">
          {events.empty_reason === 'search' ? 'По текущему поиску и фильтрам записей нет.' : 'За выбранный период платных вызовов нет.'}
        </p> : null}
        {events?.events.length ? <div className="ai-spend-table-wrap">
          {/* Раньше это была таблица с одной ячейкой на строку: `<thead>`
              со списком из шести подписей в одном `<td>`. Скринридер
              объявлял список из шести пунктов без шапки и без связи со
              строками. Теперь это список событий, а подписи ячеек
              добавляются в `.ai-spend-row.compact` на узком экране. */}
          <ul className="ai-spend-events">
            {events.events.map((event) => {
              const key = event.event_key || `${event.at}-${event.kind}-${event.entity_id}`
              const open = openKey === key
              return <li key={key} className={open ? 'ai-spend-event-row open' : 'ai-spend-event-row'}>
                <EventRow event={event} open={open} onToggle={() => setOpenKey(open ? '' : key)} />
                {open ? <EventDetails event={event} /> : null}
              </li>
            })}
          </ul>
        </div> : null}
        {events && events.pages > 1 ? <nav className="ai-spend-pager">
          <button type="button" disabled={events.page <= 1} onClick={() => setPage(events.page - 1)}>Назад</button>
          <span>Стр. {events.page} из {events.pages}</span>
          <button type="button" disabled={events.page >= events.pages} onClick={() => setPage(events.page + 1)}>Вперёд</button>
        </nav> : null}
      </section>
    </> : null}
    {openDay ? <DayJournalModal point={openDay} today={analytics?.period.today || today} onClose={() => setOpenDay(null)} /> : null}
    {filtersOpen ? <SpendFilterSheet
      kindOptions={kindOptions}
      kindFilter={kindFilter}
      metric={metric}
      onKind={setKindFilter}
      onMetric={setMetric}
      onClose={() => setFiltersOpen(false)}
    /> : null}
  </div>
}

function kindGroupFromKind(analytics: AiSpendAnalytics, kindId: string): string {
  const match = analytics.kind_groups.find((group) => group.id === kindId)
  if (match) return match.id
  if (kindId.startsWith('deal_manager_quick_help_')) return 'quick_help'
  if (kindId.startsWith('deal_manager_full_script_')) return 'scripts'
  if (kindId.startsWith('transcription')) return 'transcription'
  if (kindId === 'full_deal_analysis' || kindId === 'full_lead_analysis' || kindId === 'full_analysis' || kindId === 'incremental_deal_analysis') return 'full_analysis'
  return 'other'
}
