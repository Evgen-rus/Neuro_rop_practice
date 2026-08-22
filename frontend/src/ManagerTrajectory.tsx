import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import {
  fetchTrajectoryDay,
  fetchTrajectoryEntity,
  fetchTrajectoryEvent,
  fetchTrajectoryWindow,
  type TrajectoryBucket,
  type TrajectoryCategory,
  type TrajectoryDay,
  type TrajectoryEntity,
  type TrajectoryEvent,
  type TrajectoryEventDetail,
  type TrajectoryManager,
  type TrajectoryWindow,
} from './api'
import { formatMoscowDateTime, moscowDateInputValue } from './dateTime'

const CATEGORIES: Array<[TrajectoryCategory, string]> = [
  ['all', 'Все события'],
  ['deals', 'Deals'],
  ['leads', 'Leads'],
  ['communications', 'Коммуникации'],
  ['tasks', 'Задачи'],
  ['crm', 'CRM'],
  ['neurorop', 'НейроРОП'],
]

const LANES: Array<[Exclude<TrajectoryCategory, 'all'>, string, string]> = [
  ['deals', 'Deals', '▣'],
  ['leads', 'Leads', '♙'],
  ['communications', 'Коммуникации', '☎'],
  ['tasks', 'Задачи', '☑'],
  ['crm', 'CRM changes', '⚙'],
  ['neurorop', 'НейроРОП', '✦'],
]

function shiftDate(value: string, days: number) {
  const [year, month, day] = value.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10)
}

function longDate(value: string) {
  return new Intl.DateTimeFormat('ru-RU', {
    day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Europe/Moscow',
  }).format(new Date(`${value}T12:00:00+03:00`))
}

function timeLabel(value: string) {
  return formatMoscowDateTime(value, { hour: '2-digit', minute: '2-digit' }) || value.slice(11, 16)
}

function eventIcon(event: TrajectoryEvent) {
  if (event.category === 'neurorop') return '✦'
  if (event.label === 'Звонок') return '☎'
  if (event.label === 'Письмо' || event.label === 'Сообщение') return '✉'
  if (event.category === 'tasks') return '☑'
  if (event.event_type.includes('stage')) return '⚑'
  return '•'
}

function bucketMap(manager: TrajectoryManager) {
  return new Map(manager.buckets.map((bucket) => [bucket.from, bucket]))
}

function totalDurationLabel(seconds: number) {
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const rest = rounded % 60
  if (hours) return `${hours} ч ${minutes ? `${minutes} мин` : ''}`.trim()
  if (minutes) return `${minutes} мин ${rest ? `${rest} сек` : ''}`.trim()
  return `${rest} сек`
}

function callSummaryLabel(manager: TrajectoryManager) {
  const labels = { incoming: 'входящих', outgoing: 'исходящих', unknown: 'без направления' } as const
  const parts = (['incoming', 'outgoing', 'unknown'] as const).flatMap((direction) => {
    const item = manager.call_summary[direction]
    if (!item.count) return []
    const duration = item.count === item.missing_duration
      ? `длительность не указана`
      : totalDurationLabel(item.duration_seconds)
    const missing = item.missing_duration && item.count !== item.missing_duration
      ? ` · ${item.missing_duration} без длительности`
      : ''
    return [`${item.count} ${labels[direction]} · ${duration}${missing}`]
  })
  return `${manager.totals.calls} звонков${parts.length ? `: ${parts.join('; ')}` : ''}`
}

export function ManagerTrajectory() {
  const [date, setDate] = useState(moscowDateInputValue)
  const [bucketMinutes, setBucketMinutes] = useState<30 | 60>(60)
  const [category, setCategory] = useState<TrajectoryCategory>('all')
  const [managerId, setManagerId] = useState('')
  const [query, setQuery] = useState('')
  const [appliedQuery, setAppliedQuery] = useState('')
  const [day, setDay] = useState<TrajectoryDay | null>(null)
  const [managerOptions, setManagerOptions] = useState<Array<[string, string]>>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [windowData, setWindowData] = useState<TrajectoryWindow | null>(null)
  const [entity, setEntity] = useState<TrajectoryEntity | null>(null)
  const [loading, setLoading] = useState(true)
  const [drawerLoading, setDrawerLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedQuery(query), 260)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setWindowData(null)
    setEntity(null)
    void fetchTrajectoryDay({
      date, bucket_minutes: bucketMinutes, manager_id: managerId || undefined,
      category, q: appliedQuery,
    }).then((result) => {
      if (cancelled) return
      setDay(result)
      if (!managerId) {
        setManagerOptions(result.managers.map((manager) => [manager.manager_id, manager.manager_name]))
      }
      setExpanded((current) => current.size ? current : new Set(result.managers[0] ? [result.managers[0].manager_id] : []))
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  }, [date, bucketMinutes, managerId, category, appliedQuery])

  const gridStyle = useMemo(() => ({
    '--trajectory-columns': day?.axis.slots.length || 1,
  } as CSSProperties), [day?.axis.slots.length])

  async function openWindow(manager: TrajectoryManager, bucket: TrajectoryBucket) {
    setDrawerLoading(true)
    setEntity(null)
    setError('')
    try {
      setWindowData(await fetchTrajectoryWindow({
        manager_id: manager.manager_id,
        from: bucket.from,
        to: bucket.to,
        category,
        q: appliedQuery,
      }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setDrawerLoading(false)
    }
  }

  async function openEntity(event: TrajectoryEvent) {
    if (!event.entity_type || !event.entity_id) return
    setDrawerLoading(true)
    setError('')
    try {
      setEntity(await fetchTrajectoryEntity(event.entity_type, event.entity_id, date))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setDrawerLoading(false)
    }
  }

  function toggleManager(id: string) {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const lastSuccess = day?.collection.last_success_at
  const today = moscowDateInputValue()
  const isFutureDay = date > today
  const isCurrentDay = day?.date === date ? day.collection.is_current_day : date === today
  const updatedLabel = isFutureDay
    ? 'Данных ещё нет'
    : !isCurrentDay
      ? 'Данные за день'
      : lastSuccess
        ? `Данные на ${timeLabel(lastSuccess)}`
        : day?.collection.status === 'unknown' ? 'Сбор ещё не выполнялся' : 'Нет успешного сбора'

  return <div className="trajectory-page">
    <header className="trajectory-header">
      <div>
        <span className="dc-eyebrow">Рабочий день в Bitrix24</span>
        <h1>Траектория менеджеров</h1>
        <p>Наблюдаемая CRM-активность и временная связь с рекомендациями НейроРОПа.</p>
      </div>
      <div className={`trajectory-collection ${day?.collection.status || 'unknown'}`}>
        <span />{updatedLabel}
      </div>
    </header>

    <div className="trajectory-toolbar">
      <div className="trajectory-date-nav">
        <button type="button" onClick={() => setDate(shiftDate(date, -1))} aria-label="Предыдущий день">‹</button>
        <label><span>Дата</span><input type="date" max={today} value={date} onChange={(event) => setDate(event.target.value)} /></label>
        <button type="button" disabled={date >= today} onClick={() => setDate(shiftDate(date, 1))} aria-label="Следующий день">›</button>
        <button type="button" className="today" onClick={() => setDate(moscowDateInputValue())}>Сегодня</button>
      </div>
      <div className="trajectory-filter-row">
        <label>Шаг<select value={bucketMinutes} onChange={(event) => setBucketMinutes(Number(event.target.value) as 30 | 60)}>
          <option value={60}>1 час</option><option value={30}>30 минут</option>
        </select></label>
        <label>Менеджер<select value={managerId} onChange={(event) => setManagerId(event.target.value)}>
          <option value="">Все менеджеры</option>
          {managerOptions.map(([id, name]) => <option value={id} key={id}>{name}</option>)}
        </select></label>
        <label>События<select value={category} onChange={(event) => setCategory(event.target.value as TrajectoryCategory)}>
          {CATEGORIES.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select></label>
        <label className="trajectory-search">Поиск<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="ID или название" /></label>
      </div>
    </div>

    {error ? <div className="dc-alert error trajectory-alert">{error}</div> : null}
    {loading && !day ? <div className="trajectory-loading"><span className="dc-spinner" />Загружаем траекторию…</div> : null}

    {day ? <>
      <section className="trajectory-summary" aria-label="Итоги дня">
        {([
          ['events', 'Всего событий', '▤'], ['deals', 'Сделки', '▣'], ['leads', 'Лиды', '♙'],
          ['communications', 'Коммуникации', '☎'], ['tasks', 'Задачи', '☑'], ['neurorop', 'НейроРОП', '✦'],
        ] as const).map(([key, label, icon]) => <article key={key}>
          <span>{icon}</span><div><small>{label}</small><strong>{day.totals[key]}</strong></div>
        </article>)}
      </section>

      <section className={`trajectory-board ${loading ? 'loading' : ''}`} style={gridStyle}>
        <div className="trajectory-board-head">
          <div><strong>Обзор дня</strong><small>{longDate(date)}</small></div>
          <div className="trajectory-axis">
            {day.axis.slots.map((slot, index) => <span key={slot.from}>{index % Math.max(1, Math.round(60 / bucketMinutes)) === 0 ? slot.label : ''}</span>)}
          </div>
        </div>
        {!day.managers.length ? <div className="trajectory-empty">По выбранным фильтрам событий нет.</div> : null}
        {day.managers.map((manager) => {
          const buckets = bucketMap(manager)
          const isExpanded = expanded.has(manager.manager_id)
          return <article className={`trajectory-manager ${isExpanded ? 'expanded' : ''}`} key={manager.manager_id}>
            <div className="trajectory-manager-main">
              <button className="trajectory-manager-title" type="button" onClick={() => toggleManager(manager.manager_id)} aria-expanded={isExpanded}>
                <span>{isExpanded ? '⌃' : '⌄'}</span>
                <div><strong>{manager.manager_name}</strong><small>{manager.totals.events} событий · {manager.totals.deals} сделок · {manager.totals.leads} лидов · {manager.totals.tasks} задач · {manager.totals.communications} коммуникаций · {manager.totals.crm} CRM · {manager.totals.neurorop} НейроРОП</small><span className="trajectory-manager-call-summary">☎ {callSummaryLabel(manager)}</span></div>
              </button>
              <div className="trajectory-density-grid">
                {day.axis.slots.map((slot) => {
                  const bucket = buckets.get(slot.from)
                  return <button
                    type="button"
                    key={slot.from}
                    className={`trajectory-density ${bucket?.density || 'none'}`}
                    title={bucket ? `${slot.label}: ${bucket.count} событий` : `${slot.label}: наблюдаемой активности в Bitrix нет`}
                    aria-label={bucket ? `${manager.manager_name}, ${slot.label}, ${bucket.count} событий` : `${manager.manager_name}, ${slot.label}, наблюдаемой активности в Bitrix нет`}
                    disabled={!bucket?.count}
                    onClick={() => bucket && void openWindow(manager, bucket)}
                  ><span>{bucket?.count || ''}</span></button>
                })}
              </div>
            </div>
            <div className="trajectory-manager-stats" title="Доля наблюдаемых событий Bitrix, не оценка затраченного рабочего времени.">
              <span className="trajectory-manager-stats-label">Активность по сущностям</span>
              <span>Сделки <b>{manager.attention.distribution.deals}%</b></span>
              <span>Лиды <b>{manager.attention.distribution.leads}%</b></span>
              <span>Другое <b>{manager.attention.distribution.other}%</b></span>
              <span className="switches" title="Последовательные наблюдаемые события Bitrix по разным типам сущностей. Не является точным измерением переключения внимания человека.">Смен активной сущности Deal ↔ Lead: <b>{manager.attention.context_switches.deal_lead_total}</b></span>
            </div>
            {isExpanded ? <div className="trajectory-lanes">
              {LANES.map(([key, label, icon]) => <div className={`trajectory-lane lane-${key}`} key={key}>
                <div><span>{icon}</span><b>{label}</b><small>{manager.totals[key === 'deals' ? 'deals' : key === 'leads' ? 'leads' : key]}</small></div>
                <div className="trajectory-lane-grid">
                  {day.axis.slots.map((slot) => {
                    const bucket = buckets.get(slot.from)
                    const count = bucket?.lanes[key] || 0
                    return <button type="button" key={slot.from} disabled={!count} title={`${slot.label}: ${count || 'нет'} событий`} onClick={() => bucket && count && void openWindow(manager, bucket)}>
                      {count ? <><i /><span>{count}</span></> : null}
                    </button>
                  })}
                </div>
              </div>)}
            </div> : null}
          </article>
        })}
        <footer className="trajectory-legend">
          <span><i className="moderate" />Умеренная</span><span><i className="high" />Высокая</span><span><i className="peak" />Пиковая</span><span><i className="none" />Наблюдаемой активности в Bitrix нет</span>
        </footer>
      </section>
      <div className="trajectory-disclaimer">ⓘ Отсутствие событий означает только отсутствие наблюдаемой активности в Bitrix, а не отсутствие работы менеджера. Временная последовательность после рекомендации не доказывает причинность.</div>
    </> : null}

    {(windowData || drawerLoading) ? <aside className="trajectory-drawer" aria-label="События интервала">
      <div className="trajectory-drawer-head">
        <div>
          <small>{entity ? `${entity.entity_type.toUpperCase()} #${entity.entity_id}` : windowData?.manager_name || 'Интервал'}</small>
          <h2>{entity?.title || (windowData ? `${timeLabel(windowData.period.from)} – ${timeLabel(windowData.period.to)}` : 'Загрузка…')}</h2>
        </div>
        <button type="button" onClick={() => entity ? setEntity(null) : setWindowData(null)} aria-label={entity ? 'Назад к событиям' : 'Закрыть'}>{entity ? '←' : '×'}</button>
      </div>
      {drawerLoading ? <div className="trajectory-loading"><span className="dc-spinner" />Загрузка…</div> : null}
      {!drawerLoading && entity ? <EntityDetail entity={entity} date={date} /> : null}
      {!drawerLoading && windowData && !entity ? <div className="trajectory-event-list">
        <div className="trajectory-window-summary"><b>{windowData.events.length}</b> событий · <b>{windowData.entities}</b> сущностей</div>
        {!windowData.events.length ? <p className="trajectory-empty">В этом интервале нет событий выбранного типа.</p> : null}
        {windowData.events.map((event, index) => <TrajectoryEventRow
          event={event}
          managerId={windowData.manager_id}
          date={date}
          onOpenEntity={openEntity}
          key={`${event.event_id}-${index}`}
        />)}
      </div> : null}
    </aside> : null}
  </div>
}

function durationLabel(seconds?: number | null) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) {
    return 'длительность не указана в Bitrix'
  }
  const rounded = Math.round(seconds)
  const minutes = Math.floor(rounded / 60)
  const rest = rounded % 60
  if (!minutes) return `${rest} сек`
  return rest ? `${minutes} мин ${String(rest).padStart(2, '0')} сек` : `${minutes} мин`
}

function directionLabel(direction?: string | null) {
  const normalized = String(direction || '').toLowerCase()
  if (normalized === '1' || normalized === 'incoming') return 'Входящий'
  if (normalized === '2' || normalized === 'outgoing') return 'Исходящий'
  return 'Направление не указано'
}

function TrajectoryEventRow({
  event,
  managerId,
  date,
  onOpenEntity,
  entityContext = false,
}: {
  event: TrajectoryEvent
  managerId: string
  date: string
  onOpenEntity?: (event: TrajectoryEvent) => Promise<void>
  entityContext?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<TrajectoryEventDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const isCall = event.label === 'Звонок'
  const expandable = Boolean(event.expandable && event.event_id !== null && managerId)
  const summary = isCall
    ? `${directionLabel(event.direction)} · ${durationLabel(event.duration_seconds)}`
    : entityContext
      ? event.subject || event.description || 'Без краткого описания'
      : event.entity_title || event.subject || event.description || 'Без краткого описания'

  async function toggle() {
    if (!expandable) {
      if (event.entity_id && onOpenEntity) await onOpenEntity(event)
      return
    }
    const next = !open
    setOpen(next)
    if (!next || detail || loading || event.event_id === null) return
    setLoading(true)
    setError('')
    try {
      setDetail(await fetchTrajectoryEvent(event.event_id, managerId, date))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось раскрыть событие')
    } finally {
      setLoading(false)
    }
  }

  return <article className={`trajectory-event-wrap event-${event.category} ${open ? 'open' : ''}`}>
    <button type="button" className="trajectory-event" onClick={() => void toggle()}>
      <time>{timeLabel(event.occurred_at)}</time><span className="trajectory-event-icon">{eventIcon(event)}</span>
      <div><strong>{event.label}</strong>{!entityContext && event.entity_id ? <small className={`entity-${event.entity_type}`}>{event.entity_type?.toUpperCase()} #{event.entity_id}</small> : null}
      <p>{summary}</p>
      {event.stage_name ? <small>{event.stage_name}</small> : null}
      {event.temporal_relation ? <em>{event.temporal_relation.text}</em> : null}</div>
      {expandable ? <span className="trajectory-event-chevron">{open ? '⌃' : '⌄'}</span> : null}
    </button>
    {open ? <div className="trajectory-event-detail">
      {loading ? <p className="loading"><span className="dc-spinner" />Загружаем детали…</p> : null}
      {error ? <p className="error">{error}</p> : null}
      {detail ? <>
        {isCall ? <div className="trajectory-call-facts"><span><small>Направление</small><b>{directionLabel(detail.direction)}</b></span><span><small>Длительность</small><b>{durationLabel(detail.duration_seconds)}</b></span></div> : null}
        {detail.subject ? <p><b>{detail.subject}</b></p> : null}
        {detail.description ? <p className="trajectory-full-event-text">{detail.description}</p> : null}
        {detail.details?.length ? <dl className="trajectory-event-facts">{detail.details.map((item, index) => <div key={`${item.label}-${index}`}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl> : null}
        {detail.transcript_text ? <details className="trajectory-transcript"><summary>Расшифровка звонка</summary><pre>{detail.transcript_text}</pre>{detail.transcript_truncated ? <small>Показан первый 1 000 000 символов.</small> : null}</details> : null}
        {!entityContext && event.entity_id && onOpenEntity ? <button type="button" className="trajectory-open-entity" onClick={() => void onOpenEntity(event)}>Открыть {event.entity_type?.toUpperCase()} #{event.entity_id}</button> : null}
      </> : null}
    </div> : null}
  </article>
}

function EntityDetail({ entity, date }: { entity: TrajectoryEntity; date: string }) {
  const fields = Object.entries(entity.relevant_fields || {}).filter(([, value]) => value !== null && value !== '')
  return <div className="trajectory-entity-detail">
    <div className="trajectory-entity-meta">
      <span><small>Воронка</small><b>{entity.pipeline_name || entity.pipeline_id || '—'}</b></span>
      <span><small>Текущая стадия</small><b>{entity.stage_name || entity.stage_id || '—'}</b></span>
      <span><small>Ответственный</small><b>{entity.manager_name || `#${entity.manager_id}`}</b></span>
    </div>
    {fields.length ? <details><summary>Актуальные CRM-поля ({fields.length})</summary><dl>{fields.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></details> : null}
    <h3>Хронология дня</h3>
    {entity.created_at ? <div className="trajectory-entity-created">
      <time>{formatMoscowDateTime(entity.created_at, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</time>
      <span><b>{entity.entity_type === 'lead' ? 'Лид появился в CRM' : 'Сделка появилась в CRM'}</b><small>Системная точка начала ожидания, не действие менеджера</small></span>
    </div> : null}
    <div className="trajectory-entity-chronology">
      {entity.chronology.map((event, index) => <TrajectoryEventRow
        event={event}
        managerId={entity.manager_id || ''}
        date={date}
        entityContext
        key={`${event.event_id}-${index}`}
      />)}
    </div>
  </div>
}
