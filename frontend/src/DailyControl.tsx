import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type Ref } from 'react'
import {
  fetchDailyControlHistory,
  fetchDailyControlReport,
  setDailyControlDealReviewed,
  startDailyControlReport,
  type AuthUser,
  type DailyControlDeal,
  type DailyControlGeneration,
  type DailyControlHistory,
  type DailyControlManager,
  type DailyControlReport,
  type DailyControlSnapshot,
} from './api'
import { copyTextToClipboard } from './contextPersist'
import { formatMoscowDateTime } from './dateTime'
import { DailyIcon, DealReviewCard } from './DealReviewCard'
import { DealDetailOverlay } from './DealDetailOverlay'
import { isNarrowDealLayout, useNarrowDealLayout } from './dealOverlayLayout'
import { bitrixDealUrl, formatDealPipelineStage } from './dealDisplay'
import { DealStatusIndicator } from './dealPresentation'
import {
  businessReportWarnings,
  dailyTaskTotals,
  dealMatchesDailyTrafficStatus,
  DAILY_TRAFFIC_STATUSES,
  firstUnreviewedDeal,
  hasReportDayWork,
  matchesDailySearch,
  meaningfulAttentionReason,
  reportDayLabels,
  reportHeading,
  shouldOpenLatestReport,
  snapshotDayText,
  sortDailyReviewDeals,
  toggleDailyTrafficStatus,
  type DailyTrafficStatus,
} from './dailyControlView'
import { TaskDayResults } from './TaskDayResults'

const SPLITTER_KEY = 'neurorop-daily-control-v11-left-width'
const SPLITTER_DEFAULT = 380
const SPLITTER_MIN = 280
const SPLITTER_MAX_MARGIN = 320
const SPLITTER_STEP = 24
const EMPTY_DEALS: DailyControlDeal[] = []
const ALL_DAILY_TRAFFIC_STATUSES = new Set<DailyTrafficStatus>(DAILY_TRAFFIC_STATUSES)
const STATUS_FILTERS: Array<{ id: DailyTrafficStatus; label: string }> = [
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
      neutral: 0,
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
    if (deal.status === 'red' || deal.status === 'yellow' || deal.status === 'green') {
      current[deal.status] += 1
    } else if (deal.status === 'neutral') {
      current.neutral = (current.neutral || 0) + 1
    }
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
  const [activeStatuses, setActiveStatuses] = useState<Set<DailyTrafficStatus>>(() => new Set(ALL_DAILY_TRAFFIC_STATUSES))
  const reviewStarted = useRef(false)
  const historyPinned = useRef(false)
  const currentReportId = useRef<number | undefined>(undefined)
  const reportRequest = useRef(0)
  const [asked, setAsked] = useState<Record<string, [boolean, boolean]>>({})
  const [reviewedDealIds, setReviewedDealIds] = useState<Set<string>>(() => new Set())
  const [leftWidth, setLeftWidth] = useState(SPLITTER_DEFAULT)
  const [dragging, setDragging] = useState(false)
  const [copyNotice, setCopyNotice] = useState('')
  const layoutRef = useRef<HTMLDivElement | null>(null)
  const dealScrollRef = useRef<HTMLDivElement | null>(null)
  const selectedDealRowRef = useRef<HTMLDivElement | null>(null)
  const [offscreenDealId, setOffscreenDealId] = useState('')
  const [cardOverlayOpen, setCardOverlayOpen] = useState(false)
  const narrowDealLayout = useNarrowDealLayout()
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
    () => sortDailyReviewDeals(
      managerDeals.filter((deal) => dealMatchesDailyTrafficStatus(deal, activeStatuses)),
      reviewedDealIds,
    ),
    [activeStatuses, managerDeals, reviewedDealIds],
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
    setReviewedDealIds(new Set(payload.reviewed_deal_ids || []))
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
    setActiveStatuses(new Set(ALL_DAILY_TRAFFIC_STATUSES))
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

  useEffect(() => {
    const row = selectedDealRowRef.current
    if (!row || !selectedDeal) {
      setOffscreenDealId('')
      return
    }
    const selectedId = selectedDeal.deal_id
    // Без `root` наблюдатель смотрит во вьюпорт, а не внутрь панели:
    // список теперь растёт вместе со страницей, внутреннего скролла нет.
    const observer = new IntersectionObserver(([entry]) => {
      setOffscreenDealId(entry.isIntersecting ? '' : selectedId)
    })
    observer.observe(row)
    return () => observer.disconnect()
  }, [activeStatuses, dealId, managerId, report?.id, reviewedDealIds, search, selectedDeal, visibleDeals])

  useLayoutEffect(() => {
    // Скролл теперь у документа, поэтому сбрасываем его целиком, а не
    // только внутренний контейнер списка.
    window.scrollTo({ top: 0 })
  }, [activeStatuses, managerId, search])

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
    const wanted = String(next.manager_id || '')
    setManagerId(wanted)
    const first = firstUnreviewedDeal(
      sortDailyReviewDeals(
        searchedDeals.filter((deal) => (
          String(deal.manager_id || '') === wanted
          && dealMatchesDailyTrafficStatus(deal, activeStatuses)
        )),
        reviewedDealIds,
      ),
      reviewedDealIds,
    )
    setDealId(first?.deal_id || '')
  }

  function selectStatus(status: DailyTrafficStatus) {
    reviewStarted.current = true
    const nextStatuses = toggleDailyTrafficStatus(activeStatuses, status)
    setActiveStatuses(nextStatuses)
    const deals = sortDailyReviewDeals(
      managerDeals.filter((deal) => dealMatchesDailyTrafficStatus(deal, nextStatuses)),
      reviewedDealIds,
    )
    setDealId(firstUnreviewedDeal(deals, reviewedDealIds)?.deal_id || '')
  }

  function selectDeal(nextId: string) {
    reviewStarted.current = true
    setDealId(nextId)
    // На мобильном карточка уходит в оверлей поверх списка: иначе тап по
    // строке не даёт отклика, а карточка ждёт в самом низу страницы.
    if (isNarrowDealLayout()) setCardOverlayOpen(true)
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
      if (stillVisible.status !== 'neutral') {
        const status = stillVisible.status
        if (!activeStatuses.has(status)) {
          setActiveStatuses((current) => new Set([...current, status]))
        }
      }
      return
    }
    const target = currentHits[0] || hits[0]
    if (!target) return
    if (String(target.manager_id || '') !== wanted) {
      setManagerId(String(target.manager_id || ''))
    }
    setDealId(target.deal_id)
    if (target.status !== 'neutral') {
      const status = target.status
      if (!activeStatuses.has(status)) {
        setActiveStatuses((current) => new Set([...current, status]))
      }
    }
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

  async function toggleReviewed(dealId: string, reviewed: boolean) {
    if (!report) return
    reviewStarted.current = true
    const previousIds = reviewedDealIds
    const selectedId = selectedDeal?.deal_id || ''
    const nextIds = new Set(previousIds)
    if (reviewed) nextIds.add(dealId)
    else nextIds.delete(dealId)
    setReviewedDealIds(nextIds)
    // После «проверено» сразу открываем первую неразобранную сверху текущего списка.
    if (reviewed && dealId === selectedId) {
      const nextDeal = visibleDeals.find((item) => item.deal_id !== dealId && !previousIds.has(item.deal_id))
      if (nextDeal) setDealId(nextDeal.deal_id)
    }
    try {
      const payload = await setDailyControlDealReviewed(report.id, dealId, reviewed)
      setReviewedDealIds(new Set(payload.reviewed_deal_ids || []))
    } catch (reason) {
      setReviewedDealIds(previousIds)
      if (reviewed && dealId === selectedId) setDealId(selectedId)
      setError(reason instanceof Error ? reason.message : String(reason))
    }
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

  const newerReportAvailable = Boolean(report && history?.latest_id && history.latest_id > report.id)
  const visibleWarnings = businessReportWarnings(report?.warnings).map(snapshotDayText)
  // Разбор общий на весь срез, а не на выбранного менеджера: `reviewed_deal_ids`
  // приходит одним полем на отчёт. Знаменатель — сумма светофора, чтобы
  // «Разобрано N из 29» сходилось с «21 + 8 + 0» над ним же.
  const reviewQueueTotal = allDeals.filter((deal) => deal.status !== 'neutral').length
  const reviewQueueReviewed = useMemo(
    () => allDeals.filter((deal) => deal.status !== 'neutral' && reviewedDealIds.has(deal.deal_id)).length,
    [allDeals, reviewedDealIds],
  )
  const reviewDone = reviewQueueTotal > 0 && reviewQueueReviewed >= reviewQueueTotal
  const heading = report ? reportHeading(report) : 'Ежедневный контроль'
  const askedState: [boolean, boolean] = selectedDeal ? asked[selectedDeal.deal_id] || [false, false] : [false, false]

  if (loading && !report && !history) {
    return <div className="dc-daily-empty"><span className="dc-spinner" />Загружается ежедневный контроль…</div>
  }

  return (
    <section
      className="dc-daily"
      style={{ '--dc-daily-left': `${leftWidth}px` } as CSSProperties}
    >
      <header className="dc-daily-head">
        <div className="dc-daily-head-copy">
          <h1>{heading}</h1>
        </div>
        <div className="dc-daily-head-actions">
          <input
            type="search"
            className="dc-daily-search"
            aria-label="Поиск по сделкам всего отчёта"
            placeholder="Найти сделку, ID или задачу"
            value={search}
            onChange={(event) => applySearch(event.target.value)}
          />
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
      {generating && report ? <div className="dc-daily-banner">Формируется новый отчёт. Предыдущий срез остаётся на экране до публикации.</div> : null}
      {visibleWarnings.length ? <details className="dc-sync-errors"><summary>Проблемы с полнотой данных: {visibleWarnings.length}</summary><ul>{visibleWarnings.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}

      {!report ? <div className="dc-daily-empty">Автоматические отчёты появляются по будням в 15:45 и 23:00 МСК.{canGenerate ? ' Нажмите «Сформировать отчёт», чтобы сохранить первый срез вручную.' : ''}</div> : null}

      {snapshot ? <>
        <section className="dc-daily-team" aria-label="Итог команды за день">
          <div className="dc-daily-team-head">
            <h2>Итог команды за день</h2>
            <div className="dc-daily-team-meta">
              <small>Срез {formatClock(report?.cutoff_at) || 'нет'} · {managerCountLabel(managers.length)}</small>
              <small className="dc-daily-review-progress" role="status">
                {reviewQueueTotal > 0
                  ? <>Разобрано <b>{reviewQueueReviewed}</b> из {reviewQueueTotal}{reviewDone ? ' · разбор завершён' : ''}</>
                  : 'Нет сделок в разборе'}
              </small>
            </div>
          </div>
          <article className="dc-daily-lights" aria-label="Светофор сделок">
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
          <article className="dc-daily-metrics" aria-label="Сводка за день">
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
            const statusItems = STATUS_FILTERS.map((item) => {
              const active = activeStatuses.has(item.id)
              const label = item.id === 'red' ? 'срочно' : item.id === 'yellow' ? 'проверить' : 'в норме'
              if (!selected) {
                return (
                  <span
                    key={item.id}
                    className={`dc-daily-manager-count ${item.id}`}
                    aria-label={`${label}: ${manager[item.id]}`}
                  >
                    <i className={item.id} aria-hidden="true" />
                    {manager[item.id]}
                  </span>
                )
              }
              return (
                <span
                  key={item.id}
                  className={`dc-daily-manager-status ${item.id}${active ? ' active' : ''}`}
                  role="switch"
                  aria-checked={active}
                  aria-label={`${label}: ${active ? 'включён' : 'отключён'}`}
                  tabIndex={0}
                  onClick={(event) => {
                    event.stopPropagation()
                    selectStatus(item.id)
                  }}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter' && event.key !== ' ') return
                    event.preventDefault()
                    event.stopPropagation()
                    selectStatus(item.id)
                  }}
                >
                  <i className={item.id} aria-hidden="true" />
                  {manager[item.id]} {label}
                </span>
              )
            })
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
                <em>{statusItems}</em>
              </button>
            )
          }) : <p className="dc-daily-empty-list">В этом отчёте сделок нет.</p>}
        </section>

        <div
          className={`dc-daily-split ${dragging ? 'dragging' : ''}`}
          ref={layoutRef}
        >
          <section className="dc-daily-list" aria-label="Сделки менеджера">
            {offscreenDealId === selectedDeal?.deal_id && selectedDeal ? <button
              type="button"
              className="dc-selected-anchor"
              onClick={() => selectedDealRowRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })}
            >
              <span>Открыта</span>
              <b>{selectedDeal.title || `Сделка #${selectedDeal.deal_id}`}</b>
              <small>#{selectedDeal.deal_id}</small>
            </button> : null}
            <div className="dc-daily-deal-scroll" ref={dealScrollRef}>
              {visibleDeals.length ? visibleDeals.map((deal) => (
                <DealRow
                  key={deal.deal_id}
                  deal={deal}
                  cutoffAt={report?.cutoff_at}
                  selected={deal.deal_id === selectedDeal?.deal_id}
                  reviewed={reviewedDealIds.has(deal.deal_id)}
                  rowRef={deal.deal_id === selectedDeal?.deal_id ? selectedDealRowRef : undefined}
                  onSelect={() => selectDeal(deal.deal_id)}
                  onToggleReviewed={() => void toggleReviewed(deal.deal_id, !reviewedDealIds.has(deal.deal_id))}
                />
              )) : (
                <p className="dc-daily-empty-list">
                  {search.trim() && !searchedDeals.length
                    ? 'По поиску сделок нет.'
                    : search.trim() && !managerDeals.length
                      ? 'У этого менеджера таких сделок нет.'
                      : 'В выбранных статусах сделок нет. Включите нужный фильтр.'}
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

          {narrowDealLayout ? null : <div className="dc-daily-card-anchor">
            <DealReviewCard
              deal={selectedDeal}
              asked={askedState}
              onToggleAsked={toggleAsked}
              onCopyScript={() => void copyScript()}
              copyNotice={copyNotice}
              showStatus={false}
              snapshotDay
              snapshotCutoffAt={report.cutoff_at}
              reviewed={reviewedDealIds.has(selectedDeal?.deal_id || '')}
            />
          </div>}
        </div>
      </> : null}

      {narrowDealLayout && cardOverlayOpen && selectedDeal && report ? <DealDetailOverlay
        title={selectedDeal.title || `Сделка #${selectedDeal.deal_id}`}
        subtitle={`#${selectedDeal.deal_id} · ${selectedDeal.manager_name || 'Ответственный не указан'}`}
        onClose={() => setCardOverlayOpen(false)}
      >
        <div className="dc-daily-card-overlay-card">
          <DealReviewCard
            deal={selectedDeal}
            asked={askedState}
            onToggleAsked={toggleAsked}
            onCopyScript={() => void copyScript()}
            copyNotice={copyNotice}
            showStatus={false}
            snapshotDay
            snapshotCutoffAt={report.cutoff_at}
            reviewed={reviewedDealIds.has(selectedDeal?.deal_id || '')}
          />
        </div>
      </DealDetailOverlay> : null}
    </section>
  )
}

function DealRow({
  deal,
  cutoffAt,
  selected,
  reviewed,
  rowRef,
  onSelect,
  onToggleReviewed,
}: {
  deal: DailyControlDeal
  cutoffAt?: string
  selected: boolean
  reviewed: boolean
  rowRef?: Ref<HTMLDivElement>
  onSelect: () => void
  onToggleReviewed: () => void
}) {
  const communications = deal.communications_today
  const dayLabels = reportDayLabels(deal, cutoffAt)
  const attentionReason = meaningfulAttentionReason(deal.attention_reason)
  return (
    <div
      ref={rowRef}
      className={`dc-daily-deal ${deal.status} ${selected ? 'selected' : ''}${reviewed ? ' reviewed' : ''}`}
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
      <div className="dc-daily-deal-status">
        <DealStatusIndicator status={deal.status} label={deal.status_label} />
        <label
          className={`dc-daily-reviewed${reviewed ? ' is-on' : ''}`}
          title="Проверено РОПом"
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <input
            type="checkbox"
            checked={reviewed}
            aria-label="Проверено РОПом"
            onChange={(event) => {
              event.stopPropagation()
              onToggleReviewed()
            }}
          />
        </label>
      </div>
      <div>
        <header>
          <strong>{deal.title || `Сделка #${deal.deal_id}`}</strong>
          <b>{money(deal.amount, deal.currency_id || 'RUB')}</b>
        </header>
        <small className="dc-deal-pipeline-stage">{formatDealPipelineStage(deal)}</small>
        {dayLabels.length ? <div className="dc-daily-day-labels" aria-label="Почему сделка в отчёте и какая работа зафиксирована">
          {dayLabels.map((item) => <span className={item.kind} key={item.text}>{item.text}</span>)}
        </div> : null}
        {attentionReason
          ? <p className={selected ? 'full' : 'clamp'}>{attentionReason}</p>
          : null}
        {reviewed ? <span className="dc-daily-reviewed-mark">Проверено</span> : null}
        <footer>
          <span>{communications.unavailable ? 'Коммуникации недоступны' : `${communications.calls} звонков · ${communications.messages} сообщений за день среза${communications.conversation_duration_seconds != null ? ` · ${talkTime(communications.conversation_duration_seconds)} разговоров` : ''}`}</span>
          <a href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>Сделка #{deal.deal_id}</a>
        </footer>
        <TaskDayResults tasks={deal.task_results} cutoffAt={cutoffAt || deal.day_scope?.cutoff_at} />
      </div>
    </div>
  )
}
