import { useEffect, useMemo, useRef, useState } from 'react'
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
  type AiSpendPeriodPreset,
  type AiSpendSummary,
  type AiSpendTopEntity,
} from './api'
import { moscowDateInputValue } from './dateTime'
import {
  SPEND_CHART_HEIGHT,
  SPEND_CHART_METRICS,
  SPEND_CHART_PAD_X,
  SPEND_CHART_WIDTH,
  SPEND_PERIOD_PRESETS,
  buildAiSpendEventsQuery,
  buildAiSpendPeriodQuery,
  formatSpendDelta,
  shareWidth,
  spendChartIndexFromSvgX,
  spendChartSeries,
  spendChartValue,
  spendDayTitle,
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

function kindFilters(analytics: AiSpendAnalytics | null): { id: SpendKindFilter; label: string }[] {
  const groups = analytics?.kind_groups || []
  return [
    { id: 'all', label: 'Все' },
    ...groups.map((item) => ({ id: item.id as SpendKindFilter, label: item.label })),
  ]
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
  const values = series.map((point) => spendChartValue(point, metric))
  const max = Math.max(...values, 0)
  const width = SPEND_CHART_WIDTH
  const height = SPEND_CHART_HEIGHT
  const padX = SPEND_CHART_PAD_X
  const padY = 4
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
  const activeIndex = hover ?? (points.length ? points.length - 1 : 0)
  const active = points[activeIndex]
  const labelStep = series.length > 20 ? 6 : series.length > 10 ? 3 : 1

  function updateHover(event: { currentTarget: SVGSVGElement; clientX: number }) {
    const svgX = pointerToSvgX(event.currentTarget, event.clientX)
    if (svgX == null) return
    setHover(spendChartIndexFromSvgX(svgX, series.length, width, padX))
  }

  return <div className="ai-spend-chart-canvas">
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="Динамика расходов"
      onMouseLeave={() => setHover(null)}
      onMouseMove={updateHover}
    >
      <line className="ai-spend-chart-axis" x1={padX} y1={padY + innerH} x2={padX + innerW} y2={padY + innerH} />
      {area ? <polygon className="ai-spend-chart-area" points={area} /> : null}
      {line ? <polyline className="ai-spend-chart-line" fill="none" points={line} /> : null}
      {active ? <circle className="ai-spend-chart-dot active" cx={active.x} cy={active.y} r={4} /> : null}
    </svg>
    <div className="ai-spend-chart-labels">
      {series.map((point, index) => (
        <span key={point.date} className={index % labelStep === 0 ? '' : 'hidden'}>{point.short_label}</span>
      ))}
    </div>
    {active ? <div className="ai-spend-chart-tooltip">
      <strong>{active.point.label}</strong>
      <span>{active.point.estimated_cost_rub_label}</span>
      <span>{active.point.paid_calls_label}</span>
      <span>{active.point.total_tokens_label} токенов</span>
      {active.point.unknown_cost_calls ? <span>есть вызовы без оценки</span> : null}
    </div> : null}
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
  return <ul className="ai-spend-day-list">
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

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return <div className="ai-spend-modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section className="ai-spend-modal" role="dialog" aria-modal="true" aria-labelledby="ai-spend-day-title">
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
            <button type="button" className="ai-spend-row" onClick={() => setOpenKey(open ? '' : key)}>
              <span>{event.time}</span>
              <span>{event.entity_label || 'Без сущности'}</span>
              <span>{event.kind_label}</span>
              <span>{event.model_label || event.model || '—'}</span>
              <strong>{event.estimated_cost_rub_label}</strong>
              <em className={event.status === 'error' ? 'error' : 'ok'}>{event.status_label || event.status || '—'}</em>
            </button>
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
      <small>Расходы AI сегодня</small>
      <strong>{summary?.today.estimated_cost_rub_label || '…'}</strong>
    </div>
  </button>
}

export function AiSpend() {
  const today = moscowDateInputValue()
  const [preset, setPreset] = useState<AiSpendPeriodPreset>('30')
  const [fromDate, setFromDate] = useState(today)
  const [toDate, setToDate] = useState(today)
  const [metric, setMetric] = useState<SpendChartMetric>('cost')
  const [kindFilter, setKindFilter] = useState<SpendKindFilter>('all')
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

  const periodQuery = useMemo(
    () => buildAiSpendPeriodQuery(preset, fromDate, toDate),
    [preset, fromDate, toDate],
  )
  const eventsQuery = useMemo(
    () => buildAiSpendEventsQuery({
      preset,
      fromDate,
      toDate,
      q: debouncedSearch,
      kindGroup: kindGroupFilter,
      status: statusFilter,
      attention: attentionFilter,
      page,
      pageSize: 20,
    }),
    [preset, fromDate, toDate, debouncedSearch, kindGroupFilter, statusFilter, attentionFilter, page],
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
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
  }, [periodQuery])

  useEffect(() => {
    let cancelled = false
    setEventsError('')
    void fetchAiSpendEvents(eventsQuery)
      .then((payload) => { if (!cancelled) setEvents(payload) })
      .catch((reason) => {
        if (cancelled) return
        setEvents(null)
        setEventsError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { cancelled = true }
  }, [eventsQuery])

  useEffect(() => { setPage(1) }, [preset, fromDate, toDate, debouncedSearch, kindGroupFilter, statusFilter, attentionFilter])
  useEffect(() => { setOpenDay(null) }, [preset, fromDate, toDate, kindFilter])

  const series = spendChartSeries(analytics, kindFilter)
  const hasChart = series.some((point) => spendChartValue(point, metric) > 0 || point.unknown_cost_calls > 0)
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
        <div className="ai-spend-pills" role="tablist" aria-label="Период">
          {SPEND_PERIOD_PRESETS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={preset === item.id ? 'active' : ''}
              onClick={() => setPreset(item.id)}
            >{item.label}</button>
          ))}
        </div>
        {preset === 'custom' ? <div className="ai-spend-custom">
          <input type="date" value={fromDate} max={toDate || today} onChange={(event) => setFromDate(event.target.value)} />
          <span>—</span>
          <input type="date" value={toDate} min={fromDate} max={today} onChange={(event) => setToDate(event.target.value)} />
        </div> : null}
        <small>{analytics?.period.label || '…'}</small>
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
            <div className="ai-spend-pills" aria-label="Тип операций">
              {kindFilters(analytics).map((item) => (
                <button key={item.id} type="button" className={kindFilter === item.id ? 'active' : ''} onClick={() => setKindFilter(item.id)}>
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          <div className="ai-spend-pills compact">
            {SPEND_CHART_METRICS.map((item) => (
              <button key={item.id} type="button" className={metric === item.id ? 'active' : ''} onClick={() => setMetric(item.id)}>
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
          <span>{events ? `${events.total} записей` : ''}</span>
        </header>
        <div className="ai-spend-journal-tools">
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Сделка, лид, модель, run_id, job_id"
          />
          <select value={kindGroupFilter} onChange={(event) => setKindGroupFilter(event.target.value)}>
            <option value="">Все операции</option>
            {analytics.kind_groups.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">Все статусы</option>
            <option value="success">успешно</option>
            <option value="error">ошибка</option>
          </select>
          {filtersActive ? <button type="button" className="ai-spend-clear" onClick={() => {
            setSearch('')
            setDebouncedSearch('')
            setKindGroupFilter('')
            setStatusFilter('')
            setAttentionFilter('')
          }}>Сбросить</button> : null}
        </div>
        {attentionFilter ? <p className="ai-spend-filter-note">Показаны события: {analytics.attention.find((item) => item.id === attentionFilter)?.title || attentionFilter}.</p> : null}
        {eventsError ? <p className="dc-alert error">{eventsError}</p> : null}
        {!events && !eventsError ? <p className="ai-spend-empty">Загружаем журнал…</p> : null}
        {events && !events.events.length ? <p className="ai-spend-empty">
          {events.empty_reason === 'search' ? 'По текущему поиску и фильтрам записей нет.' : 'За выбранный период платных вызовов нет.'}
        </p> : null}
        {events?.events.length ? <div className="ai-spend-table-wrap">
          <table className="ai-spend-table">
            <thead>
              <tr>
                <td>
                  <div className="ai-spend-row head">
                    <span>Время</span>
                    <span>Сущность</span>
                    <span>Операция</span>
                    <span>Модель</span>
                    <span>Стоимость</span>
                    <span>Статус</span>
                  </div>
                </td>
              </tr>
            </thead>
            <tbody>
              {events.events.map((event) => {
                const key = event.event_key || `${event.at}-${event.kind}-${event.entity_id}`
                const open = openKey === key
                return <tr key={key} className={open ? 'open' : ''}>
                  <td>
                    <button type="button" className="ai-spend-row" onClick={() => setOpenKey(open ? '' : key)}>
                      <span>{event.datetime_label || event.time}</span>
                      <span>{event.entity_label || 'Без сущности'}</span>
                      <span>{event.kind_label}</span>
                      <span>{event.model_label || event.model || '—'}</span>
                      <strong>{event.estimated_cost_rub_label}</strong>
                      <em className={event.status === 'error' ? 'error' : 'ok'}>{event.status_label || event.status || '—'}</em>
                    </button>
                    {open ? <EventDetails event={event} /> : null}
                  </td>
                </tr>
              })}
            </tbody>
          </table>
        </div> : null}
        {events && events.pages > 1 ? <nav className="ai-spend-pager">
          <button type="button" disabled={events.page <= 1} onClick={() => setPage(events.page - 1)}>Назад</button>
          <span>Стр. {events.page} из {events.pages}</span>
          <button type="button" disabled={events.page >= events.pages} onClick={() => setPage(events.page + 1)}>Вперёд</button>
        </nav> : null}
      </section>
    </> : null}
    {openDay ? <DayJournalModal point={openDay} today={analytics?.period.today || today} onClose={() => setOpenDay(null)} /> : null}
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
