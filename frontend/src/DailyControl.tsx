import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react'
import {
  fetchDailyControlHistory,
  fetchDailyControlReport,
  startDailyControlReport,
  type AuthUser,
  type DailyControlDeal,
  type DailyControlGeneration,
  type DailyControlHistory,
  type DailyControlManager,
  type DailyControlReport,
  type DailyControlSnapshot,
  type DailyControlStatus,
} from './api'
import { copyTextToClipboard } from './contextPersist'
import { formatMoscowDateTime, parseMoscowDateTime } from './dateTime'
import { DailyIcon, DealReviewCard } from './DealReviewCard'
import { bitrixDealUrl, formatDealPipelineStage } from './dealDisplay'
import { DealStatusIndicator } from './dealPresentation'

const SPLITTER_KEY = 'neurorop-daily-control-v11-left-width'
const SPLITTER_DEFAULT = 380
const SPLITTER_MIN = 280
const SPLITTER_MAX_MARGIN = 320
const SPLITTER_STEP = 24
const STATUS_FILTERS: Array<{ id: 'all' | DailyControlStatus; label: string }> = [
  { id: 'all', label: 'Все' },
  { id: 'red', label: 'Красные' },
  { id: 'yellow', label: 'Жёлтые' },
  { id: 'green', label: 'Зелёные' },
]
type DailyControlTimeFilter = 'all' | 'today' | 'tomorrow' | 'future'
const TIME_FILTERS: Array<{ id: DailyControlTimeFilter; label: string; hint: string }> = [
  { id: 'all', label: 'Все', hint: 'Все сделки команды в этом отчёте' },
  { id: 'today', label: 'Сегодня', hint: 'Задачи на сегодня, уже просроченные и сделки без открытой задачи Bitrix' },
  { id: 'tomorrow', label: 'Завтра', hint: 'Сделки с открытой задачей Bitrix на завтра' },
  { id: 'future', label: 'Будущие', hint: 'Сделки с открытой задачей Bitrix позже завтра' },
]
const OPEN_BITRIX_TIME_BUCKETS = new Set(['overdue', 'today', 'tomorrow', 'future', 'unscheduled', 'missing'])

function formatClock(value?: string | null) {
  if (!value) return ''
  return formatMoscowDateTime(value, { hour: '2-digit', minute: '2-digit' }) || ''
}

function formatHeadline(value?: string | null) {
  if (!value) return 'Сохранённого отчёта пока нет'
  const datePart = formatMoscowDateTime(value, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  })
  const timePart = formatClock(value)
  if (!datePart || !timePart) return value
  const pretty = `${datePart.charAt(0).toUpperCase()}${datePart.slice(1)}`
  return `${pretty} · срез на ${timePart} МСК`
}

function managerCountLabel(count: number) {
  const mod10 = count % 10
  const mod100 = count % 100
  if (mod10 === 1 && mod100 !== 11) return `${count} менеджер`
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} менеджера`
  return `${count} менеджеров`
}

function sameMinute(left?: string | null, right?: string | null) {
  if (!left || !right) return true
  const first = parseMoscowDateTime(left)
  const second = parseMoscowDateTime(right)
  if (Number.isNaN(first.getTime()) || Number.isNaN(second.getTime())) return left === right
  return Math.abs(first.getTime() - second.getTime()) < 60_000
}

function money(value?: string | number | null, currency = 'RUB') {
  const parsed = Number(String(value ?? '').replace(',', '.'))
  if (!Number.isFinite(parsed)) return '—'
  return new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency: currency || 'RUB',
    maximumFractionDigits: 0,
  }).format(parsed)
}

function talkTime(seconds?: number | null) {
  const total = Math.max(0, Math.round(Number(seconds || 0)))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}`
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

function talkDuration(seconds?: number | null) {
  const total = Math.max(0, Math.round(Number(seconds || 0)))
  if (!total) return '0 мин'
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  if (hours > 0) return `${hours} ч ${String(minutes).padStart(2, '0')} мин`
  return rest ? `${minutes} мин ${rest} сек` : `${minutes} мин`
}

function readStoredWidth() {
  try {
    const raw = window.localStorage.getItem(SPLITTER_KEY)
    const parsed = Number(raw)
    return Number.isFinite(parsed) && parsed >= SPLITTER_MIN ? parsed : SPLITTER_DEFAULT
  } catch {
    return SPLITTER_DEFAULT
  }
}

function dealTimeBucket(deal: DailyControlDeal) {
  const value = deal.bitrix_task_time_bucket
  return typeof value === 'string' && OPEN_BITRIX_TIME_BUCKETS.has(value) ? value : null
}

function snapshotHasTimeBuckets(deals: DailyControlDeal[]) {
  return deals.some((deal) => dealTimeBucket(deal) !== null)
}

function dealMatchesTime(deal: DailyControlDeal, filter: DailyControlTimeFilter) {
  if (filter === 'all') return true
  const bucket = dealTimeBucket(deal) || 'missing'
  // Пункт «Сегодня» специально включает просроченные и сделки без открытой задачи Bitrix.
  if (filter === 'today') return bucket === 'missing' || bucket === 'overdue' || bucket === 'today'
  if (filter === 'tomorrow') return bucket === 'tomorrow'
  return bucket === 'future' || bucket === 'unscheduled'
}

function summarizeDailyControl(deals: DailyControlDeal[]): {
  team: DailyControlSnapshot['team']
  managers: DailyControlManager[]
} {
  const managersById = new Map<string, DailyControlManager>()
  let noMovement = 0
  let movementScope = 0
  for (const deal of deals) {
    const managerId = String(deal.manager_id || '') || 'unassigned'
    const current = managersById.get(managerId) || {
      manager_id: deal.manager_id,
      manager_name: deal.manager_name || 'Без ответственного',
      deals_count: 0,
      checklist_completed: 0,
      checklist_total: 0,
      calls: 0,
      messages: 0,
      talk_seconds: 0,
      red: 0,
      yellow: 0,
      green: 0,
    }
    current.deals_count += 1
    current.checklist_completed += Number(deal.checklist?.completed || 0)
    current.checklist_total += Number(deal.checklist?.total || 0)
    const communications = deal.communications_today
    if (communications?.unavailable) {
      /* Коммуникации недоступны: это не нулевая активность. */
    } else {
      movementScope += 1
      if (Number(communications?.completed || 0) === 0) noMovement += 1
      current.calls += Number(communications?.calls || 0)
      current.messages += Number(communications?.messages || 0)
      current.talk_seconds += Number(communications?.duration_seconds || 0)
    }
    current[deal.status] += 1
    managersById.set(managerId, current)
  }
  const managers = [...managersById.values()].sort((left, right) => (
    (right.red - left.red)
    || (right.yellow - left.yellow)
    || left.manager_name.localeCompare(right.manager_name, 'ru')
    || String(left.manager_id || '').localeCompare(String(right.manager_id || ''))
  ))
  return {
    team: {
      traffic_light: {
        red: deals.filter((deal) => deal.status === 'red').length,
        yellow: deals.filter((deal) => deal.status === 'yellow').length,
        green: deals.filter((deal) => deal.status === 'green').length,
      },
      deals_total: deals.length,
      no_movement: { count: noMovement, total: movementScope },
      calls: managers.reduce((sum, item) => sum + item.calls, 0),
      messages: managers.reduce((sum, item) => sum + item.messages, 0),
      talk_seconds: managers.reduce((sum, item) => sum + item.talk_seconds, 0),
    },
    managers,
  }
}

export function DailyControl({ user: _user }: { user: AuthUser }) {
  const [history, setHistory] = useState<DailyControlHistory | null>(null)
  const [report, setReport] = useState<DailyControlReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [generation, setGeneration] = useState<DailyControlGeneration | null>(null)
  const [managerId, setManagerId] = useState('')
  const [dealId, setDealId] = useState('')
  const [filter, setFilter] = useState<'all' | DailyControlStatus>('all')
  const [timeFilter, setTimeFilter] = useState<DailyControlTimeFilter>('all')
  const [asked, setAsked] = useState<Record<string, [boolean, boolean]>>({})
  const [leftWidth, setLeftWidth] = useState(SPLITTER_DEFAULT)
  const [dragging, setDragging] = useState(false)
  const [copyNotice, setCopyNotice] = useState('')
  const [openEventId, setOpenEventId] = useState('')
  const layoutRef = useRef<HTMLDivElement | null>(null)
  const dealScrollRef = useRef<HTMLDivElement | null>(null)
  const cardAnchorRef = useRef<HTMLDivElement | null>(null)
  const previousDealId = useRef(dealId)
  const generating = generation?.status === 'running' || generation?.status === 'queued'

  const snapshot = report?.snapshot
  const allDeals = snapshot?.deals
  const hasTimeBuckets = snapshotHasTimeBuckets(allDeals || [])
  const showTimeFilter = hasTimeBuckets && report?.freshness?.is_latest === true
  const activeTimeFilter: DailyControlTimeFilter = showTimeFilter ? timeFilter : 'all'
  const timeFilteredDeals = useMemo(
    () => (allDeals || []).filter((deal) => dealMatchesTime(deal, activeTimeFilter)),
    [activeTimeFilter, allDeals],
  )
  const { team, managers } = useMemo(
    () => summarizeDailyControl(timeFilteredDeals),
    [timeFilteredDeals],
  )
  const selectedManager = managers.find((item) => String(item.manager_id || '') === managerId) || managers[0] || null
  const managerDeals = useMemo(() => {
    const wanted = String(selectedManager?.manager_id || '')
    return timeFilteredDeals.filter((deal) => String(deal.manager_id || '') === wanted)
  }, [selectedManager, timeFilteredDeals])
  const visibleDeals = useMemo(
    () => managerDeals.filter((deal) => filter === 'all' || deal.status === filter),
    [filter, managerDeals],
  )
  const selectedDeal = visibleDeals.find((deal) => deal.deal_id === dealId) || visibleDeals[0] || null

  const loadHistory = useCallback(async () => {
    const payload = await fetchDailyControlHistory()
    setHistory(payload)
    setGeneration(payload.generation)
    return payload
  }, [])

  const loadReport = useCallback(async (id: number) => {
    const payload = await fetchDailyControlReport(id)
    setReport(payload)
    setGeneration(payload.generation || null)
    return payload
  }, [])

  useEffect(() => {
    setLeftWidth(readStoredWidth())
  }, [])

  useEffect(() => {
    let cancelled = false
    async function boot() {
      setLoading(true)
      setError('')
      try {
        const payload = await loadHistory()
        if (cancelled) return
        if (payload.latest_id) await loadReport(payload.latest_id)
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void boot()
    return () => { cancelled = true }
  }, [loadHistory, loadReport])

  useEffect(() => {
    setTimeFilter('all')
  }, [report?.id])

  useEffect(() => {
    if (!generating) return
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const payload = await loadHistory()
          if (payload.generation?.status === 'done' && payload.generation.report_id) {
            await loadReport(payload.generation.report_id)
          }
          if (payload.generation?.status === 'error') {
            setError(payload.generation.error || 'Не удалось сформировать отчёт')
          }
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      })()
    }, 1500)
    return () => window.clearInterval(timer)
  }, [generating, loadHistory, loadReport])

  useEffect(() => {
    if (!snapshot) return
    const nextManager = selectedManager?.manager_id ? String(selectedManager.manager_id) : ''
    if (nextManager !== managerId) setManagerId(nextManager)
    const nextDeal = selectedDeal?.deal_id || ''
    if (nextDeal && nextDeal !== dealId) setDealId(nextDeal)
  }, [dealId, managerId, selectedDeal, selectedManager, snapshot])

  useLayoutEffect(() => {
    const previous = previousDealId.current
    previousDealId.current = dealId
    if (!previous || !dealId || previous === dealId) return
    cardAnchorRef.current?.scrollIntoView({ block: 'start' })
  }, [dealId])

  useLayoutEffect(() => {
    dealScrollRef.current?.scrollTo({ top: 0 })
  }, [filter, managerId, timeFilter])

  useEffect(() => {
    if (!dragging) return
    const move = (event: PointerEvent) => {
      const rect = layoutRef.current?.getBoundingClientRect()
      if (!rect) return
      const next = Math.min(rect.width - SPLITTER_MAX_MARGIN, Math.max(SPLITTER_MIN, event.clientX - rect.left))
      setLeftWidth(next)
    }
    const up = () => {
      setDragging(false)
      try { window.localStorage.setItem(SPLITTER_KEY, String(leftWidth)) } catch { /* ignore */ }
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [dragging, leftWidth])

  function selectManager(next: DailyControlManager) {
    setManagerId(String(next.manager_id || ''))
    setFilter('all')
    const first = timeFilteredDeals.find((deal) => String(deal.manager_id || '') === String(next.manager_id || ''))
    setDealId(first?.deal_id || '')
  }

  function selectFilter(next: 'all' | DailyControlStatus) {
    setFilter(next)
    const deals = managerDeals.filter((deal) => next === 'all' || deal.status === next)
    setDealId(deals[0]?.deal_id || '')
  }

  function selectDeal(nextId: string) {
    if (nextId === dealId) {
      cardAnchorRef.current?.scrollIntoView({ block: 'start' })
      return
    }
    setDealId(nextId)
  }

  function startReview() {
    setTimeFilter('all')
    setFilter('all')
    const sourceManagers = snapshot?.managers || []
    const redManager = sourceManagers.find((item) => item.red > 0) || sourceManagers[0]
    if (!redManager) return
    setManagerId(String(redManager.manager_id || ''))
    const redDeal = (snapshot?.deals || []).find(
      (deal) => String(deal.manager_id || '') === String(redManager.manager_id || '') && deal.status === 'red',
    ) || (snapshot?.deals || []).find((deal) => String(deal.manager_id || '') === String(redManager.manager_id || ''))
    setDealId(redDeal?.deal_id || '')
  }

  async function openReport(id: number | null | undefined) {
    if (!id) return
    setError('')
    setLoading(true)
    try {
      await loadReport(id)
      await loadHistory()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }

  async function generate() {
    if (generating) return
    setError('')
    try {
      const started = await startDailyControlReport()
      setGeneration(started)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  function toggleAsked(index: 0 | 1) {
    if (!selectedDeal) return
    setAsked((current) => {
      const previous = current[selectedDeal.deal_id] || [false, false]
      const next: [boolean, boolean] = [...previous]
      next[index] = !next[index]
      return { ...current, [selectedDeal.deal_id]: next }
    })
  }

  function onSplitterKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const rect = layoutRef.current?.getBoundingClientRect()
    const max = rect ? rect.width - SPLITTER_MAX_MARGIN : leftWidth + SPLITTER_STEP
    const delta = event.key === 'ArrowLeft' ? -SPLITTER_STEP : SPLITTER_STEP
    const next = Math.min(max, Math.max(SPLITTER_MIN, leftWidth + delta))
    setLeftWidth(next)
    try { window.localStorage.setItem(SPLITTER_KEY, String(next)) } catch { /* ignore */ }
  }

  async function copyScript() {
    if (!selectedDeal) return
    const script = String(selectedDeal.ai_context.manager_coaching || '').trim()
    if (!script) return
    const copied = await copyTextToClipboard(script)
    setCopyNotice(copied ? 'Сценарий скопирован' : 'Скопировать не удалось — выделите текст вручную')
    window.setTimeout(() => setCopyNotice(''), 2500)
  }

  const freshness = report?.freshness
  const sourceTag = report?.creation_kind === 'automatic_planning' ? 'Авто · к планёрке' : report ? 'Вручную' : ''
    const askedState: [boolean, boolean] = selectedDeal ? asked[selectedDeal.deal_id] || [false, false] : [false, false]
  const managerCounts = {
    all: managerDeals.length,
    red: managerDeals.filter((deal) => deal.status === 'red').length,
    yellow: managerDeals.filter((deal) => deal.status === 'yellow').length,
    green: managerDeals.filter((deal) => deal.status === 'green').length,
  }

  if (loading && !report && !history) {
    return <div className="dc-daily-empty"><span className="dc-spinner" />Загружается ежедневный контроль…</div>
  }

  return (
    <section className="dc-daily">
      <header className="dc-daily-head">
        <div className="dc-daily-head-copy">
          <h1>Ежедневный контроль</h1>
          <p>
            {formatHeadline(report?.cutoff_at)}
            {report && !sameMinute(report.cutoff_at, report.created_at) && formatClock(report.created_at)
              ? ` · сформирован ${formatClock(report.created_at)}`
              : ''}
            {sourceTag ? <span className={`dc-daily-source ${report?.creation_kind || ''}`}>{sourceTag}</span> : null}
            {freshness ? <span className={`dc-daily-freshness ${freshness.state}`}>{freshness.label}</span> : null}
          </p>
        </div>
        <div className="dc-daily-head-actions">
          <nav className="dc-daily-history" aria-label="История отчётов">
            <button type="button" disabled={!report?.previous_id} onClick={() => void openReport(report?.previous_id)} aria-label="Предыдущий отчёт">←</button>
            <span>{report?.position || 0} из {report?.total || history?.total || 0}</span>
            <button type="button" disabled={!report?.next_id} onClick={() => void openReport(report?.next_id)} aria-label="Следующий отчёт">→</button>
          </nav>
          {showTimeFilter ? (
            <select
              className="dc-daily-time-filter"
              aria-label="Срок открытой задачи Bitrix"
              title={TIME_FILTERS.find((item) => item.id === activeTimeFilter)?.hint}
              value={activeTimeFilter}
              onChange={(event) => setTimeFilter(event.target.value as DailyControlTimeFilter)}
            >
              {TIME_FILTERS.map((item) => (
                <option value={item.id} key={item.id} title={item.hint}>
                  {item.label}
                </option>
              ))}
            </select>
          ) : null}
          <button type="button" className="dc-button primary" onClick={startReview} disabled={!snapshot}>Начать разбор</button>
          <button type="button" className="dc-button" onClick={() => void generate()} disabled={generating}>
            {generating ? <><span className="dc-spinner" />Формируем отчёт…</> : 'Сформировать отчёт'}
          </button>
        </div>
      </header>

      {error ? <div className="dc-alert error">{error}</div> : null}
      {generating && report ? <div className="dc-daily-banner">Формируется новый отчёт. Предыдущий срез остаётся на экране до публикации.</div> : null}
      {freshness?.state === 'stale' ? <div className="dc-daily-banner warn">После этого среза появились более свежие данные. Можно сформировать новый отчёт.</div> : null}
      {report?.warnings?.length ? <details className="dc-sync-errors"><summary>Часть источников была недоступна: {report.warnings.length}</summary><ul>{report.warnings.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}

      {!report ? <div className="dc-daily-empty">Нажмите «Сформировать отчёт», чтобы сохранить первый срез. В рабочий день к 15:45 МСК появится автоматический отчёт к планёрке.</div> : null}

      {snapshot ? <>
        <section className="dc-daily-team" aria-label="Итог команды за день">
          <div className="dc-daily-team-head">
            <h2>Итог команды за день</h2>
            <small>Срез {formatClock(report?.cutoff_at) || 'нет'} · {managerCountLabel(managers.length)}</small>
          </div>
          <article className="dc-daily-lights">
            <header>
              <span>Светофор сделок</span>
              <small>приоритет РОПа</small>
            </header>
            <div className="dc-daily-traffic">
              <div className="dc-daily-lamp" aria-hidden="true">
                <span className="red">{team.traffic_light.red}</span>
                <span className="yellow">{team.traffic_light.yellow}</span>
                <span className="green">{team.traffic_light.green}</span>
              </div>
              <ul>
                <li className="red"><b>{team.traffic_light.red}</b><span>Срочно <small>решить с РОПом</small></span></li>
                <li className="yellow"><b>{team.traffic_light.yellow}</b><span>Проверить <small>нужен контроль</small></span></li>
                <li className="green"><b>{team.traffic_light.green}</b><span>В норме <small>движется по плану</small></span></li>
              </ul>
            </div>
          </article>
          <article className="dc-daily-metrics">
            <header>
              <span>Итог за день</span>
              <small>сделки и активность</small>
            </header>
            <div>
              <div>
                <span className="dc-daily-metric-icon"><DailyIcon name="briefcase" /></span>
                <span><strong>{team.deals_total}</strong><small>Всего сделок</small></span>
              </div>
              <div>
                <span className="dc-daily-metric-icon"><DailyIcon name="pause" /></span>
                <span><strong>{team.no_movement.count} из {team.no_movement.total || team.deals_total}</strong><small>Без движения</small></span>
              </div>
              <div>
                <span className="dc-daily-metric-icon"><DailyIcon name="phone" /></span>
                <span><strong>{team.calls}</strong><small>Звонков</small></span>
              </div>
              <div>
                <span className="dc-daily-metric-icon"><DailyIcon name="message" /></span>
                <span><strong>{team.messages}</strong><small>Сообщений</small></span>
              </div>
              <div>
                <span className="dc-daily-metric-icon"><DailyIcon name="clock" /></span>
                <span><strong>{talkTime(team.talk_seconds)}</strong><small>В разговорах</small></span>
              </div>
            </div>
          </article>
        </section>

        <section className="dc-daily-managers" aria-label="Менеджеры">
          {managers.length ? managers.map((manager) => {
            const id = String(manager.manager_id || '')
            const selected = id === String(selectedManager?.manager_id || '')
            return (
              <button
                type="button"
                key={id || manager.manager_name}
                className={selected ? 'selected' : ''}
                role="tab"
                aria-selected={selected}
                onClick={() => selectManager(manager)}
              >
                <strong>{manager.manager_name}</strong>
                <span>{manager.checklist_completed} из {manager.checklist_total} задач</span>
                <small>{manager.deals_count} сделок · {manager.calls} звонков · {manager.messages} сообщений · {talkDuration(manager.talk_seconds)}</small>
                <em aria-label={`${manager.red} срочно, ${manager.yellow} проверить, ${manager.green} в норме`}>
                  <i className="red" aria-hidden="true" />{manager.red} срочно
                  <b aria-hidden="true">·</b>
                  <i className="yellow" aria-hidden="true" />{manager.yellow} проверить
                  <b aria-hidden="true">·</b>
                  <i className="green" aria-hidden="true" />{manager.green} в норме
                </em>
              </button>
            )
          }) : <p className="dc-daily-empty-list">В выбранном сроке сделок нет.</p>}
        </section>

        <div
          className={`dc-daily-split ${dragging ? 'dragging' : ''}`}
          ref={layoutRef}
          style={{ '--dc-daily-left': `${leftWidth}px` } as CSSProperties}
        >
          <section className="dc-daily-list" aria-label="Сделки менеджера">
            {selectedManager ? (
              <header>
                <div>
                  <h2>{selectedManager.manager_name}</h2>
                  <p>{selectedManager.deals_count} сделок · {selectedManager.calls} звонков · {selectedManager.messages} сообщений · {talkTime(selectedManager.talk_seconds)}</p>
                </div>
              </header>
            ) : null}
            <div className="dc-daily-filters">
              {STATUS_FILTERS.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  className={filter === item.id ? 'active' : ''}
                  aria-pressed={filter === item.id}
                  onClick={() => selectFilter(item.id)}
                >
                  {item.label} · {managerCounts[item.id]}
                </button>
              ))}
            </div>
            <div className="dc-daily-deal-scroll" ref={dealScrollRef}>
              {visibleDeals.length ? visibleDeals.map((deal) => (
                <DealRow
                  key={deal.deal_id}
                  deal={deal}
                  selected={deal.deal_id === selectedDeal?.deal_id}
                  onSelect={() => selectDeal(deal.deal_id)}
                />
              )) : <p className="dc-daily-empty-list">В этой категории сделок нет. Выберите другой фильтр.</p>}
            </div>
          </section>

          <div
            className="dc-daily-resizer"
            role="separator"
            aria-orientation="vertical"
            aria-label="Изменить ширину панелей"
            tabIndex={0}
            onPointerDown={(event) => { event.preventDefault(); setDragging(true) }}
            onKeyDown={onSplitterKey}
          />

          <div className="dc-daily-card-anchor" ref={cardAnchorRef}>
            <DealReviewCard
              deal={selectedDeal}
              asked={askedState}
              onToggleAsked={toggleAsked}
              onCopyScript={() => void copyScript()}
              copyNotice={copyNotice}
              openEventId={openEventId}
              onToggleEvent={(eventId) => setOpenEventId((current) => current === eventId ? '' : eventId)}
            />
          </div>
        </div>
      </> : null}
    </section>
  )
}

function DealRow({ deal, selected, onSelect }: { deal: DailyControlDeal; selected: boolean; onSelect: () => void }) {
  const communications = deal.communications_today
  return (
    <button type="button" className={`dc-daily-deal ${deal.status} ${selected ? 'selected' : ''}`} role="tab" aria-selected={selected} onClick={onSelect}>
      <DealStatusIndicator status={deal.status} label={deal.status_label} />
      <div>
        <header>
          <strong>{deal.title || `Сделка #${deal.deal_id}`}</strong>
          <b>{money(deal.amount, deal.currency_id || 'RUB')}</b>
        </header>
        <small className="dc-deal-pipeline-stage">{formatDealPipelineStage(deal)}</small>
        <p className={selected ? 'full' : 'clamp'}>{deal.attention_reason}</p>
        <footer>
          <span>{communications.unavailable ? 'Коммуникации недоступны' : `${communications.completed} касаний сегодня · ${talkTime(communications.duration_seconds)} разговоров`}</span>
          <a href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>Сделка #{deal.deal_id}</a>
        </footer>
      </div>
    </button>
  )
}
