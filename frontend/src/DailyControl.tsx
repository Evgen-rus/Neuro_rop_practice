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
import { dailyTaskTotals, matchesDailySearch, hasReportDayWork, reportDayLabels, reportHeading, shouldOpenLatestReport } from './dailyControlView'
import { TaskDayResults } from './TaskDayResults'

const SPLITTER_KEY = 'neurorop-daily-control-v11-left-width'
const SPLITTER_DEFAULT = 380
const SPLITTER_MIN = 280
const SPLITTER_MAX_MARGIN = 320
const SPLITTER_STEP = 24
const EMPTY_DEALS: DailyControlDeal[] = []
const STATUS_FILTERS: Array<{ id: 'all' | DailyControlStatus; label: string }> = [
  { id: 'all', label: 'Все' },
  { id: 'red', label: 'Красные' },
  { id: 'yellow', label: 'Жёлтые' },
  { id: 'green', label: 'Зелёные' },
]

function formatClock(value?: string | null) {
  if (!value) return ''
  return formatMoscowDateTime(value, { hour: '2-digit', minute: '2-digit' }) || ''
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
      calls: 0,
      messages: 0,
      talk_seconds: 0,
      red: 0,
      yellow: 0,
      green: 0,
    }
    current.deals_count += 1
    const communications = deal.communications_today
    const hasWork = hasReportDayWork(deal)
    if (hasWork || !communications?.unavailable) {
      movementScope += 1
      if (!hasWork) noMovement += 1
    }
    if (communications?.unavailable) {
      /* Коммуникации недоступны: это не нулевая активность. */
    } else {
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
  for (const manager of managers) Object.assign(manager, dailyTaskTotals(deals.filter((deal) => deal.manager_id === manager.manager_id)))
  return {
    team: {
      ...dailyTaskTotals(deals),
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

export function DailyControl({ user }: { user: AuthUser }) {
  const [search, setSearch] = useState('')
  const canGenerate = user.role === 'admin'
  const [history, setHistory] = useState<DailyControlHistory | null>(null)
  const [report, setReport] = useState<DailyControlReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [generation, setGeneration] = useState<DailyControlGeneration | null>(null)
  const [managerId, setManagerId] = useState('')
  const [dealId, setDealId] = useState('')
  const [filter, setFilter] = useState<'all' | DailyControlStatus>('all')
  const reviewStarted = useRef(false)
  const historyPinned = useRef(false)
  const currentReportId = useRef<number | undefined>(undefined)
  const reportRequest = useRef(0)
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
  const allDeals = snapshot?.deals || EMPTY_DEALS
  const searchedDeals = useMemo(
    () => allDeals.filter((deal) => matchesDailySearch(deal, search)),
    [allDeals, search],
  )
  const { team, managers } = useMemo(
    () => summarizeDailyControl(allDeals),
    [allDeals],
  )
  const selectedManager = managers.find((item) => String(item.manager_id || '') === managerId) || managers[0] || null
  const managerDeals = useMemo(() => {
    const wanted = String(selectedManager?.manager_id || '')
    return searchedDeals.filter((deal) => String(deal.manager_id || '') === wanted)
  }, [selectedManager, searchedDeals])
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

  const loadReport = useCallback(async (id: number, background = false) => {
    const request = ++reportRequest.current
    const payload = await fetchDailyControlReport(id)
    if (reportRequest.current !== request || (background && (reviewStarted.current || historyPinned.current))) return payload
    currentReportId.current = id
    setReport(payload)
    reviewStarted.current = false
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
        const initialId = payload.default_id || payload.latest_id
        if (initialId) await loadReport(initialId)
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
    setAsked({})
    setSearch('')
    setFilter('all')
  }, [report?.id])

  useEffect(() => {
    let cancelled = false
    let checking = false
    async function checkLatest() {
      if (cancelled || checking || document.visibilityState === 'hidden') return
      checking = true
      const viewedId = currentReportId.current
      try {
        const payload = await fetchDailyControlHistory()
        if (cancelled || currentReportId.current !== viewedId) return
        setHistory(payload)
        const defaultId = payload.default_id || payload.latest_id
        if (shouldOpenLatestReport(viewedId, defaultId, reviewStarted.current, historyPinned.current)) {
          await loadReport(defaultId!, true)
        } else if (payload.latest_id === viewedId) {
          // Refresh freshness only: never replace the frozen snapshot during a review.
          const meta = payload.reports.find((item) => item.id === viewedId)
          if (meta?.freshness) setReport((previous) => previous?.id === viewedId ? { ...previous, freshness: meta.freshness! } : previous)
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        checking = false
      }
    }
    const onReturn = () => { void checkLatest() }
    window.addEventListener('focus', onReturn)
    document.addEventListener('visibilitychange', onReturn)
    const timer = window.setInterval(onReturn, 60_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      window.removeEventListener('focus', onReturn)
      document.removeEventListener('visibilitychange', onReturn)
    }
  }, [loadReport])

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
  }, [filter, managerId, search])

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
    reviewStarted.current = true
    setManagerId(String(next.manager_id || ''))
    setFilter('all')
    const wanted = String(next.manager_id || '')
    const first = searchedDeals.find((deal) => String(deal.manager_id || '') === wanted)
    setDealId(first?.deal_id || '')
  }

  function selectFilter(next: 'all' | DailyControlStatus) {
    reviewStarted.current = true
    setFilter(next)
    const deals = managerDeals.filter((deal) => next === 'all' || deal.status === next)
    setDealId(deals[0]?.deal_id || '')
  }

  function selectDeal(nextId: string) {
    reviewStarted.current = true
    if (nextId === dealId) {
      cardAnchorRef.current?.scrollIntoView({ block: 'start' })
      return
    }
    setDealId(nextId)
  }

  // Поиск идёт по всему отчёту. Если совпадение у другого менеджера — открываем его и эту сделку.
  function applySearch(next: string) {
    reviewStarted.current = true
    setSearch(next)
    const needle = next.trim()
    if (!needle) return
    const hits = allDeals.filter((deal) => matchesDailySearch(deal, next))
    const wanted = String(managerId || '')
    const currentHits = hits.filter((deal) => String(deal.manager_id || '') === wanted)
    const stillVisible = currentHits.find((deal) => deal.deal_id === dealId)
    if (stillVisible) {
      setFilter((current) => (current === 'all' || stillVisible.status === current ? current : 'all'))
      return
    }
    const target = currentHits[0] || hits[0]
    if (!target) return
    if (String(target.manager_id || '') !== wanted) {
      setManagerId(String(target.manager_id || ''))
    }
    setDealId(target.deal_id)
    setFilter((current) => (current === 'all' || target.status === current ? current : 'all'))
  }

  async function openReport(id: number | null | undefined) {
    if (!id) return
    historyPinned.current = id !== history?.latest_id
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
    if (generating || !canGenerate) return
    historyPinned.current = false
    setError('')
    try {
      const started = await startDailyControlReport()
      setGeneration(started)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  function toggleAsked(index: 0 | 1) {
    reviewStarted.current = true
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
  const newerReportAvailable = Boolean(report && history?.latest_id && history.latest_id > report.id)
  const preparation = snapshot?.source_preparation
  const preparationFinished = preparation?.status === 'done' && preparation.finished_at
  const preparationReady = Boolean(preparationFinished && preparation?.business_date === report?.business_date)
  const legacyDayScope = allDeals.some((deal) => deal.day_scope?.legacy)
  const heading = report ? reportHeading(report) : 'Ежедневный контроль'
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
          <h1>{heading}</h1>
          <p>
            {report && !sameMinute(report.cutoff_at, report.created_at) && formatClock(report.created_at)
              ? `сформирован ${formatClock(report.created_at)}`
              : null}
            {freshness ? <span className={`dc-daily-freshness ${freshness.state}`}>{freshness.label}</span> : null}
          </p>
        </div>
        <div className="dc-daily-head-actions">
          <nav className="dc-daily-history" aria-label="История отчётов">
            <button type="button" disabled={!report?.previous_id} onClick={() => void openReport(report?.previous_id)} aria-label="Предыдущий отчёт">←</button>
            <span>{report?.position || 0} из {report?.total || history?.total || 0}</span>
            <button type="button" disabled={!report?.next_id} onClick={() => void openReport(report?.next_id)} aria-label="Следующий отчёт">→</button>
          </nav>
          {canGenerate ? <button type="button" className="dc-button" onClick={() => void generate()} disabled={generating}>
            {generating ? <><span className="dc-spinner" />Формируем отчёт…</> : 'Сформировать отчёт'}
          </button> : null}
        </div>
      </header>

      {error ? <div className="dc-alert error">{error}</div> : null}
      {history?.missing_morning_final ? <div className="dc-daily-banner warn">Итоговый отчёт за предыдущий рабочий день ещё не найден. На экране — доступный сохранённый срез. Автоматические отчёты формируются по будням в 15:45 и 23:00 МСК.</div> : null}
      {newerReportAvailable ? <div className="dc-daily-banner">Появился новый отчёт. Текущий разбор сохранён на экране. <button type="button" className="dc-button" onClick={() => void openReport(history?.latest_id)}>Открыть новый отчёт</button></div> : null}
      {legacyDayScope ? <div className="dc-daily-banner">Старый срез: отбор и подписи используют только сохранённые в нём сроки, коммуникации и отметки. История стадий, комментариев и задач из текущей базы не добавляется.</div> : null}
      {generating && report ? <div className="dc-daily-banner">Формируется новый отчёт. Предыдущий срез остаётся на экране до публикации.</div> : null}
      {freshness?.state === 'stale' ? <div className="dc-daily-banner">После этого среза появились более свежие данные. Сохранённый отчёт не меняется.{canGenerate ? ' Можно сформировать новый отчёт.' : ''}</div> : null}
      {preparation ? <div className={`dc-daily-banner dc-daily-prep${preparationReady ? '' : ' warn'}`}>
        {preparationReady
          ? `Анализ к срезу: последний автоматический пакет завершён ${formatMoscowDateTime(preparation.finished_at || '', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })} МСК. Новый AI перед этим отчётом не запускался.`
          : preparation.status === 'running'
            ? 'Анализ к срезу: автоматический пакет ещё выполнялся. Отчёт опубликован вовремя, без ожидания модели. После завершения пакета можно сформировать новый отчёт.'
            : preparation.status === 'error' || preparation.status === 'interrupted'
              ? 'Анализ к срезу: автоматический пакет не завершился успешно. Отчёт собран из последних сохранённых данных, без нового AI.'
              : 'Анализ к срезу: успешное завершение автоматического пакета за сегодня не подтверждено. Использованы последние сохранённые данные, без нового AI.'}
      </div> : null}
      {report?.warnings?.length ? <details className="dc-sync-errors"><summary>Оговорки об актуальности и доступности данных: {report.warnings.length}</summary><ul>{report.warnings.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}

      {!report ? <div className="dc-daily-empty">Автоматические отчёты появляются по будням в 15:45 и 23:00 МСК.{canGenerate ? ' Нажмите «Сформировать отчёт», чтобы сохранить первый срез вручную.' : ''}</div> : null}

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
              <div><span><strong>{allDeals?.some((deal) => deal.task_results !== undefined) ? team.tasks_completed : '—'}</strong><small>Задач выполнено за день</small></span></div>
              <div><span><strong>{allDeals?.some((deal) => deal.task_results !== undefined) ? team.tasks_rescheduled : '—'}</strong><small>Задач перенесено за день</small></span></div>
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
          }) : <p className="dc-daily-empty-list">В этом отчёте сделок нет.</p>}
        </section>

        <div
          className={`dc-daily-split ${dragging ? 'dragging' : ''}`}
          ref={layoutRef}
          style={{ '--dc-daily-left': `${leftWidth}px` } as CSSProperties}
        >
          <section className="dc-daily-list" aria-label="Сделки менеджера">
            <header>
              <div>
                <div className="dc-daily-list-head-row">
                  <h2>{selectedManager?.manager_name || 'Сделки отчёта'}</h2>
                  <input
                    className="dc-daily-search"
                    type="search"
                    aria-label="Поиск по сделкам всего отчёта"
                    placeholder="Найти сделку, ID или задачу"
                    value={search}
                    onChange={(event) => applySearch(event.target.value)}
                    onClick={(event) => event.stopPropagation()}
                  />
                </div>
                {selectedManager ? (
                  <>
                    <p>{selectedManager.deals_count} сделок · {selectedManager.calls} звонков · {selectedManager.messages} сообщений · {talkTime(selectedManager.talk_seconds)}</p>
                    <p>Работа за {formatMoscowDateTime(report!.business_date, { day: 'numeric', month: 'long' })} · до {formatClock(report?.cutoff_at)} МСК</p>
                  </>
                ) : null}
              </div>
            </header>
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
              )) : (
                <p className="dc-daily-empty-list">
                  {search.trim() && !searchedDeals.length
                    ? 'По поиску сделок нет.'
                    : search.trim() && !managerDeals.length
                      ? 'У этого менеджера таких сделок нет.'
                      : 'В этой категории сделок нет. Выберите другой фильтр.'}
                </p>
              )}
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
  const dayLabels = reportDayLabels(deal)
  const untouched = Boolean(deal.day_scope?.untouched)
  return (
    <div
      className={`dc-daily-deal ${deal.status} ${selected ? 'selected' : ''}`}
      role="tab"
      tabIndex={0}
      aria-selected={selected}
      onClick={onSelect}
      onKeyDown={(event) => {
        const tag = (event.target as HTMLElement).tagName
        if (tag === 'A' || tag === 'SUMMARY' || tag === 'DETAILS' || tag === 'INPUT') return
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      }}
    >
      <DealStatusIndicator status={deal.status} label={deal.status_label} />
      <div>
        <header>
          <strong>{deal.title || `Сделка #${deal.deal_id}`}</strong>
          <b>{money(deal.amount, deal.currency_id || 'RUB')}</b>
        </header>
        <small className="dc-deal-pipeline-stage">{formatDealPipelineStage(deal)}</small>
        {dayLabels.length ? <div className="dc-daily-day-labels" aria-label="Почему сделка в отчёте и какая работа зафиксирована">
          {dayLabels.map((item) => <span className={item.kind} key={item.text}>{item.text}</span>)}
        </div> : null}
        {deal.day_scope && !untouched && !hasReportDayWork(deal) ? <small className="dc-daily-work-note">Работа за этот день в срезе не зафиксирована</small> : null}
        <p className={selected ? 'full' : 'clamp'}>{deal.attention_reason}</p>
        <footer>
          <span>{communications.unavailable ? 'Коммуникации недоступны' : `${communications.calls} звонков · ${communications.messages} сообщений за день среза · ${talkTime(communications.duration_seconds)} разговоров`}</span>
          <a href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>Сделка #{deal.deal_id}</a>
        </footer>
        <TaskDayResults tasks={deal.task_results} />
      </div>
    </div>
  )
}
