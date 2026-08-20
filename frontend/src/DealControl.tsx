import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import {
  confirmManagerSituation,
  fetchAutomaticAnalysisLatest,
  fetchDealControl,
  fetchManagerAssistantWorkspace,
  fetchManagerFullScript,
  fetchManagerFullScriptJob,
  fetchManagerFollowups,
  fetchManagerFollowupsJob,
  fetchManagerCompanion,
  fetchManagerCompanionJob,
  fetchManagerQuickHelpJob,
  fetchManagerSituationJob,
  fetchReportMarkdown,
  fetchJob,
  recordManagerCommunicationCompleted,
  recordRecommendationEvent,
  saveDealControlScope,
  startAnalyze,
  startManagerQuickHelp,
  startManagerFullScript,
  startManagerFollowups,
  startManagerCompanion,
  startManagerSituationRefinement,
  syncDealControl,
  transcribeManagerVoice,
  updateDealControlDeal,
  updateDealControlBitrixTaskCompletion,
  updateDealControlChecklistItemCompletion,
  updateDealContextLeverPriority,
  type DealControlDashboard,
  type DealControlBitrixTask,
  type AutomaticAnalysisLatest,
  type DealControlCommunicationsToday,
  type DealControlDeal,
  type DealControlTask,
  type DealControlRecommendationState,
  type DealContextSnapshot,
  type AuthUser,
  type JobState,
  type ManagerQuickHelpContent,
  type ManagerQuickHelpEntry,
  type ManagerQuickHelpJob,
  type ManagerQuickHelpLegacyContent,
  type ManagerQuickHelpStrategy,
  type ManagerQuickHelpStrategyContent,
  type ManagerQuickHelpStrategyV2Content,
  type ManagerQuickHelpStrategyV3Content,
  type ManagerAssistantMode,
  type ManagerCallScriptContent,
  type ManagerFullScriptBlock,
  type ManagerFullScriptJob,
  type ManagerFullScriptMode,
  type ManagerFullScriptWorkspace,
  type ManagerObjectionHandling,
  type ManagerFollowupsJob,
  type ManagerFollowupsRecord,
  type ManagerCompanionJob,
  type ManagerCompanionLastContact,
  type ManagerCompanionRecord,
  type ManagerAssistantWorkspace,
  type ManagerDiscProfile,
  type ManagerSituationJob,
  type ManagerSituationState,
  isCallScriptContent,
  isNeuroRopTask,
} from './api'
import { formatMoscowDateTime, moscowDateParts, parseMoscowDateTime } from './dateTime'
import {
  AUTOMATIC_ANALYSIS_IDLE_POLL_MS,
  automaticAnalysisCountersText,
  automaticAnalysisCurrentText,
  automaticAnalysisPollInterval,
  automaticAnalysisStageLabel,
  automaticAnalysisStatusLabel,
  shouldReloadAfterAutomaticAnalysis,
} from './automaticAnalysis'
import {
  freshQuickHelpIdFromJob,
  latestQuickHelpEntryId,
  revealClassName,
  shouldAnimateQuickHelpAnswer,
} from './quickHelpReveal'
import { useQuickHelpReveal } from './useQuickHelpReveal'
import {
  answerModeClassName,
  assistantAnswerPane,
  currentEntryForMode,
  entryForTurn,
  missingCurrentModes,
  pressureLever,
  quickHelpAnswerReady,
  sharedTurns,
  strategyLabel,
  visibleLifehack,
  workspaceModeClassName,
} from './dealPush'
import { copyTextToClipboard, persistTextAndOpenUrl } from './contextPersist'
import { DailyControl } from './DailyControl'
import { DealReviewCard } from './DealReviewCard'

type DealControlView = 'dashboard' | 'rop' | 'daily' | 'manager'
type TimeView = 'all' | 'attention' | 'today' | 'tomorrow' | 'future' | 'overdue'

const BITRIX_DEAL_BASE_URL = 'https://obtorg.bitrix24.ru/crm/deal/details'
const BITRIX_ORIGIN = 'https://obtorg.bitrix24.ru'

const EXECUTION_LABELS: Record<DealControlTask['crm_execution_status'], string> = {
  not_reflected: 'Не отражено в Bitrix',
  crm_open: 'Есть открытая задача',
  crm_closed: 'Задача закрыта в Bitrix',
  match_review: 'Проверить совпадение',
}

const VIEW_COPY: Record<DealControlView, { title: string; subtitle: string }> = {
  dashboard: {
    title: 'Дашборд',
    subtitle: 'Быстрый обзор всех сделок, их состояния и ключевых метрик',
  },
  rop: {
    title: 'Контроль РОП',
    subtitle: 'Что просрочено, что на сегодня и как помочь менеджеру довести сделку',
  },
  daily: {
    title: 'Ежедневный контроль',
    subtitle: 'Срез команды к планёрке: кого разбирать и какие вопросы задать',
  },
  manager: {
    title: 'Мои задачи',
    subtitle: 'Все касания в одном месте: что сделать, что выяснить и как провести разговор',
  },
}

const ANALYSIS_STAGE_LABELS: Record<string, string> = {
  queued: 'Ожидает запуска',
  crm_context: 'Собираем историю сделки из Bitrix',
  audio_lookup: 'Ищем записи звонков',
  audio_download: 'Загружаем недостающие аудио',
  transcription: 'Транскрибируем разговоры',
  llm_analysis: 'LLM анализирует сделку',
  validation: 'Проверяем результат модели',
  report: 'Сохраняем отчёт',
  skipped: 'Новых значимых изменений нет — используем предыдущий анализ',
  done: 'Анализ готов',
  error: 'Анализ завершился с ошибкой',
}

const ANALYSIS_STAGE_PROGRESS: Record<string, number> = {
  queued: 5,
  crm_context: 15,
  audio_lookup: 25,
  audio_download: 35,
  transcription: 50,
  llm_analysis: 72,
  validation: 86,
  report: 95,
  skipped: 100,
  done: 100,
  error: 100,
}

function splitIds(value: string) {
  return value.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean)
}

function bitrixDealUrl(dealId: string) {
  return `${BITRIX_DEAL_BASE_URL}/${encodeURIComponent(dealId)}/`
}

function bitrixTaskUrl(task: DealControlBitrixTask) {
  if (!task.task_id || !task.responsible_id) return null
  return `${BITRIX_ORIGIN}/company/personal/user/${encodeURIComponent(task.responsible_id)}/tasks/task/view/${encodeURIComponent(task.task_id)}/`
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

function dateTime(value?: string | null) {
  if (!value) return 'Не назначен'
  return formatMoscowDateTime(value, {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }) || value
}

function dateTimeParts(value?: string | null) {
  if (!value) return null
  const date = formatMoscowDateTime(value, { day: '2-digit', month: '2-digit' })
  const time = formatMoscowDateTime(value, { hour: '2-digit', minute: '2-digit' })
  if (!date || !time) return { date: value, time: '' }
  return {
    date,
    time,
  }
}

const PAYMENT_MONTHS = [
  ['янв', 0], ['фев', 1], ['мар', 2], ['апр', 3], ['май', 4], ['июн', 5],
  ['июл', 6], ['авг', 7], ['сен', 8], ['окт', 9], ['ноя', 10], ['дек', 11],
] as const

function paymentMonthOptions() {
  const current = moscowDateParts()
  const currentMonthIndex = current.year * 12 + current.month - 1
  return Array.from({ length: 6 }, (_, offset) => {
    const absoluteMonth = currentMonthIndex + offset
    const year = Math.floor(absoluteMonth / 12)
    const monthIndex = absoluteMonth % 12
    const month = PAYMENT_MONTHS[monthIndex][0]
    return {
      value: `${year}-${String(monthIndex + 1).padStart(2, '0')}`,
      label: `${month}.${year === current.year ? '' : ` ${year}`}`,
    }
  })
}

function parsePaymentPeriod(value?: string | null) {
  const normalized = String(value || '').toLocaleLowerCase('ru')
  const week = normalized.match(/([1-5])\s*нед/)?.[1] || ''
  const year = Number(normalized.match(/20\d{2}/)?.[0] || moscowDateParts().year)
  const monthEntry = PAYMENT_MONTHS.find(([token]) => normalized.includes(token))
  const month = monthEntry ? `${year}-${String(monthEntry[1] + 1).padStart(2, '0')}` : ''
  return { week, month }
}

function formatPaymentPeriod(week: string, month: string) {
  const parts: string[] = []
  if (week) parts.push(`${week} нед.`)
  if (month) {
    const [yearText, monthText] = month.split('-')
    const monthIndex = Number(monthText) - 1
    const monthLabel = PAYMENT_MONTHS.find(([, index]) => index === monthIndex)?.[0]
    if (monthLabel) parts.push(`${monthLabel}. ${yearText}`)
  }
  return parts.join(', ') || null
}

function dateOnly(value?: string | null) {
  if (!value) return '—'
  return formatMoscowDateTime(value, {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  }) || value
}

function stageAge(value?: string | null) {
  if (!value) return null
  const parsed = parseMoscowDateTime(value)
  if (Number.isNaN(parsed.getTime())) return null
  return Math.max(0, Math.floor((Date.now() - parsed.getTime()) / 86_400_000))
}

function taskStatus(task?: DealControlTask | null) {
  if (!task) return 'Нет поручения'
  if (task.local_status === 'completed') return 'Выполнено'
  if (task.local_status === 'cancelled') return 'Отменено'
  if (task.time_bucket === 'overdue') return 'Просрочено'
  if (task.time_bucket === 'today') return 'На сегодня'
  if (task.time_bucket === 'tomorrow') return 'На завтра'
  return EXECUTION_LABELS[task.crm_execution_status] || 'Будущее'
}

function taskTone(task?: DealControlTask | null) {
  if (!task) return 'future'
  if (task.local_status === 'completed') return 'done'
  if (task.time_bucket === 'overdue') return 'overdue'
  if (task.time_bucket === 'today') return 'today'
  return 'future'
}

function currentTaskOf(deal: DealControlDeal): DealControlTask | null {
  void deal
  return null
}

function neuroRopTaskOf(deal: DealControlDeal): DealControlTask | null {
  if (isNeuroRopTask(deal.current_task) && deal.current_task?.local_status === 'active') {
    return deal.current_task
  }
  return (deal.tasks || []).find((task) => isNeuroRopTask(task) && task.local_status === 'active') || null
}

const allowedLegacyOutcomeEvidence = new Set(['transcript', 'manager_confirmation', 'rop_confirmation'])

function hasAllowedLegacyOutcomeEvidence(task: DealControlTask) {
  const outcome = task.latest_outcome
  return Boolean(
    outcome
      && allowedLegacyOutcomeEvidence.has(outcome.evidence_kind || '')
      && outcome.result_note?.trim(),
  )
}

function recommendationStateOf(task: DealControlTask): DealControlRecommendationState {
  const backendState = task.recommendation_state
  if (backendState) return backendState
  if (!isNeuroRopTask(task) || task.local_status !== 'active') return 'unconfirmed'

  // Legacy responses have no backend state. CRM activity can only establish
  // an attempt; contact/achievement need explicit allowed evidence.
  const outcome = task.latest_outcome
  const explicitContact = outcome?.contact_status === 'confirmed_contact' && hasAllowedLegacyOutcomeEvidence(task)
  if (outcome?.result_status === 'achieved' && explicitContact) return 'achieved'
  if (explicitContact) return 'contacted'
  if (outcome?.contact_status === 'attempt_no_contact'
    || Boolean(task.crm_facts?.some((fact) => fact.contact_class === 'attempt' && fact.review_status !== 'rejected'))) {
    return 'attempted'
  }
  return 'unconfirmed'
}

function recommendationRankOf(deal: DealControlDeal) {
  const task = neuroRopTaskOf(deal)
  if (!task) return 4
  return ({ not_done: 0, attempted: 1, contacted: 2, unconfirmed: 2, achieved: 3 } as Record<DealControlRecommendationState, number>)[recommendationStateOf(task)]
}

function primaryBitrixTaskOf(deal: DealControlDeal) {
  return deal.primary_bitrix_task || deal.bitrix_tasks?.[0] || null
}

function controlTimeBucket(deal: DealControlDeal): string {
  return primaryBitrixTaskOf(deal)?.time_bucket || 'missing'
}

function timeRank(deal: DealControlDeal) {
  return ({ missing: 0, overdue: 1, today: 2, tomorrow: 3, future: 4, unscheduled: 5 } as Record<string, number>)[controlTimeBucket(deal) || ''] ?? 6
}

function dealMatchesTime(deal: DealControlDeal, view: TimeView) {
  if (view === 'all') return true
  const bucket = controlTimeBucket(deal)
  if (view === 'attention') return bucket === 'missing' || bucket === 'overdue'
  if (view === 'today') return bucket === 'missing' || bucket === 'today' || bucket === 'overdue'
  if (view === 'future') return bucket === 'future' || bucket === 'unscheduled'
  return bucket === view
}

function bitrixTaskTone(task: DealControlBitrixTask) {
  if (task.completion_state === 'local' || task.completion_state === 'bitrix') return 'done'
  if (task.time_bucket === 'overdue') return 'overdue'
  if (task.time_bucket === 'today') return 'today'
  return 'done'
}

function taskPlanTitle(view: TimeView) {
  if (view === 'overdue') return 'Просроченные задачи'
  if (view === 'tomorrow') return 'План на завтра'
  if (view === 'future') return 'Будущие задачи'
  if (view === 'all') return 'Все задачи'
  return 'План на сегодня'
}

function compactTaskText(value: string, maxLength = 120) {
  const normalized = value.replace(/\s+/g, ' ').trim()
  const firstSentence = normalized.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim()
  if (firstSentence && firstSentence.length <= maxLength) return firstSentence
  if (normalized.length <= maxLength) return normalized
  const clipped = normalized.slice(0, maxLength)
  const lastSpace = clipped.lastIndexOf(' ')
  return `${clipped.slice(0, lastSpace > 70 ? lastSpace : maxLength).trim()}…`
}

function bitrixTaskDisplayTitle(deal: DealControlDeal, task: DealControlBitrixTask) {
  const subject = task.subject.replace(/^CRM:\s*/i, '').replace(/\s+/g, ' ').trim()
  const dealTitle = (deal.title || '').replace(/\s+/g, ' ').trim()
  if (dealTitle && subject.slice(0, dealTitle.length).toLocaleLowerCase('ru') === dealTitle.toLocaleLowerCase('ru')) {
    const remainder = subject.slice(dealTitle.length).replace(/^\s*\/\s*/, '').trim()
    if (remainder) return compactTaskText(remainder, 140)
  }
  return compactTaskText(subject, 140)
}

function bitrixTaskDeadline(task: DealControlBitrixTask) {
  if (!task.deadline) return { label: 'Без срока', value: '' }
  const date = formatMoscowDateTime(task.deadline, { day: '2-digit', month: '2-digit' }) || ''
  const time = formatMoscowDateTime(task.deadline, { hour: '2-digit', minute: '2-digit' }) || ''
  if (task.time_bucket === 'today') return { label: 'Сегодня', value: time }
  if (task.time_bucket === 'tomorrow') return { label: 'Завтра', value: time }
  if (task.time_bucket === 'overdue') return { label: 'Просрочено', value: `${date}${time ? `, ${time}` : ''}` }
  return { label: date || 'Срок', value: time }
}

const MANAGER_SITUATION_DRAFT_PREFIX = 'rop-assistant:manager-situation:'
const MANAGER_QUICK_HELP_DRAFT_PREFIX = 'rop-assistant:manager-quick-help:'

function managerSituationOf(deal: DealControlDeal): ManagerSituationState {
  return deal.manager_situation || deal.coaching.manager_situation || {
    state: 'pending',
    source_report_id: deal.coaching.report_id || null,
    is_current: false,
  }
}

function managerSituationIsConfirmed(situation: ManagerSituationState) {
  return situation.is_current && situation.state === 'confirmed'
}

function readDealDraft(prefix: string, dealId: string) {
  if (!dealId || typeof window === 'undefined') return ''
  try {
    return window.localStorage.getItem(`${prefix}${dealId}`) || ''
  } catch {
    return ''
  }
}

function writeDealDraft(prefix: string, dealId: string, value: string) {
  if (!dealId || typeof window === 'undefined') return
  try {
    if (value) window.localStorage.setItem(`${prefix}${dealId}`, value)
    else window.localStorage.removeItem(`${prefix}${dealId}`)
  } catch {
    // Local storage can be disabled in private or embedded browser contexts.
  }
}

function discProfileLabel(profile: ManagerDiscProfile | null | undefined) {
  if (!profile) return 'DISC: недостаточно данных'
  const styles = [profile.primary_style, profile.secondary_style].filter(Boolean).join('/')
  const confidence = profile.profile_confidence === 'high' ? 'высокая' : profile.profile_confidence === 'medium' ? 'средняя' : 'низкая'
  return `DISC: ${styles} · уверенность: ${confidence}`
}

function appendVoiceText(current: string, transcript: string) {
  const next = transcript.trim()
  if (!next) return current
  if (!current.trim()) return next
  return `${current.trim()}\n${next}`
}

const NOTICE_TOAST_MS = 20_000

function NoticeToast({ message, onClose }: { message: string; onClose: () => void }) {
  return createPortal(
    <div className="dc-toast" role="status">
      <p>{message}</p>
      <button type="button" className="dc-toast-close" aria-label="Закрыть уведомление" onClick={onClose}>×</button>
    </div>,
    document.body,
  )
}

function AutomaticAnalysisStatus({ onReportsPublished }: { onReportsPublished: () => void }) {
  const [snapshot, setSnapshot] = useState<AutomaticAnalysisLatest | null>(null)
  const previousSnapshotRef = useRef<AutomaticAnalysisLatest | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const poll = async () => {
      try {
        const payload = await fetchAutomaticAnalysisLatest()
        if (cancelled) return
        const latest = payload.latest
        setSnapshot(latest)
        if (shouldReloadAfterAutomaticAnalysis(previousSnapshotRef.current, latest)) {
          onReportsPublished()
        }
        previousSnapshotRef.current = latest
        window.clearTimeout(timer)
        timer = window.setTimeout(() => void poll(), automaticAnalysisPollInterval(latest?.status))
      } catch {
        if (cancelled) return
        window.clearTimeout(timer)
        timer = window.setTimeout(() => void poll(), AUTOMATIC_ANALYSIS_IDLE_POLL_MS)
      }
    }
    void poll()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [onReportsPublished])

  if (!snapshot) return null
  const current = automaticAnalysisCurrentText(snapshot)
  const stage = current ? null : automaticAnalysisStageLabel(snapshot.current_stage)
  const updated = snapshot.updated_at || snapshot.started_at
  return (
    <div className={`dc-auto-analysis ${snapshot.status}`} role="status">
      {snapshot.status === 'running' ? <span className="dc-spinner" /> : null}
      <div>
        <strong>{automaticAnalysisStatusLabel(snapshot.status)}</strong>
        {current ? <span className="dc-auto-analysis-current">{current}</span> : null}
        <small>
          {snapshot.business_date ? `${snapshot.business_date} · ` : ''}
          {automaticAnalysisCountersText(snapshot)}
          {stage ? ` · этап: ${stage}` : ''}
          {updated ? ` · обновлено ${dateTime(updated)}` : ''}
        </small>
      </div>
    </div>
  )
}

export function DealControl({ onExit, onLogout, user }: { onExit?: () => void; onLogout?: () => Promise<void>; user: AuthUser }) {
  const defaultView: DealControlView = user.role === 'manager' ? 'dashboard' : user.role === 'rop' ? 'rop' : 'dashboard'
  const [data, setData] = useState<DealControlDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [syncStatus, setSyncStatus] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [view, setView] = useState<DealControlView>(defaultView)
  const [selectedId, setSelectedId] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const [managerFilter, setManagerFilter] = useState('')
  const [stageFilter, setStageFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [search, setSearch] = useState('')
  const [timeView, setTimeView] = useState<TimeView>('all')
  const [leftWidth, setLeftWidth] = useState(65)
  const [dragging, setDragging] = useState(false)
  const layoutRef = useRef<HTMLDivElement | null>(null)
  const [initialIds, setInitialIds] = useState('')
  const [managerIds, setManagerIds] = useState('')
  const [analysisJob, setAnalysisJob] = useState<JobState | null>(null)
  const [analyzingDealId, setAnalyzingDealId] = useState('')
  const [analysisConfirmDeal, setAnalysisConfirmDeal] = useState<DealControlDeal | null>(null)
  const canOpenRopView = user.role === 'admin' || user.role === 'rop'
  const canOpenManagerView = user.role === 'admin' || user.role === 'manager'

  function openDashboard() {
    setView('dashboard')
    setManagerFilter('')
    setTimeView('all')
  }

  function openRopView() {
    setView('rop')
    setManagerFilter('')
    setTimeView('today')
  }

  function openDailyView() {
    setView('daily')
    setManagerFilter('')
    setTimeView('all')
  }

  function openManagerView() {
    setView('manager')
    setManagerFilter(user.role === 'manager'
      ? user.manager_id || ''
      : selected?.manager_id || managers[0]?.[0] || '')
    setTimeView('today')
  }

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const response = await fetchDealControl()
      setData(response)
      setInitialIds(response.scope.initial_deal_ids.join('\n'))
      setManagerIds(response.scope.manager_ids.join('\n'))
      setSelectedId((current) => {
        if (current && response.deals.some((deal) => deal.deal_id === current && deal.can_open)) return current
        return response.deals.find((deal) => deal.can_open)?.deal_id || ''
      })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  useEffect(() => {
    if (!dragging) return
    const move = (event: PointerEvent) => {
      const rect = layoutRef.current?.getBoundingClientRect()
      if (!rect) return
      const value = ((event.clientX - rect.left) / rect.width) * 100
      setLeftWidth(Math.min(76, Math.max(43, value)))
    }
    const stop = () => setDragging(false)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
    }
  }, [dragging])

  const analysisJobId = analysisJob?.job_id
  const analysisJobStatus = analysisJob?.status
  useEffect(() => {
    if (!analysisJobId || !['queued', 'running'].includes(analysisJobStatus || '')) return
    let cancelled = false
    let terminalHandled = false
    const poll = async () => {
      try {
        const next = await fetchJob(analysisJobId)
        if (cancelled) return
        setAnalysisJob(next)
        if (!terminalHandled && next.status === 'done') {
          terminalHandled = true
          const progress = entityAnalysisProgress(next, analyzingDealId)
          setNotice(progress?.stage === 'skipped'
            ? 'Новых значимых данных нет — текущий анализ актуален.'
            : `Анализ сделки #${analyzingDealId} завершён. Карточка обновлена.`)
          await reload()
        } else if (!terminalHandled && next.status === 'error') {
          terminalHandled = true
          await reload()
          setError(next.error || `Не удалось завершить анализ сделки #${analyzingDealId}`)
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1800)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [analysisJobId, analysisJobStatus, analyzingDealId, reload])

  const managers = useMemo(() => {
    const values = new Map<string, string>()
    data?.deals.forEach((deal) => {
      if (deal.manager_id) values.set(deal.manager_id, deal.manager_name || `Ответственный #${deal.manager_id}`)
    })
    return [...values.entries()].sort((a, b) => a[1].localeCompare(b[1], 'ru'))
  }, [data])

  const stages = useMemo(
    () => [...new Set((data?.deals || []).map((deal) => deal.stage_name).filter(Boolean) as string[])].sort(),
    [data],
  )

  const filteredDeals = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase('ru')
    return [...(data?.deals || [])]
      .filter((deal) => !managerFilter || deal.manager_id === managerFilter)
      .filter((deal) => !stageFilter || deal.stage_name === stageFilter)
      .filter((deal) => {
        const bitrixTask = primaryBitrixTaskOf(deal)
        if (statusFilter === 'pending') return !bitrixTask || bitrixTask.completion_state === 'open'
        if (statusFilter === 'completed') return bitrixTask?.completion_state === 'local' || bitrixTask?.completion_state === 'bitrix'
        if (statusFilter === 'overdue') return controlTimeBucket(deal) === 'overdue'
        return true
      })
      .filter((deal) => {
        if (!needle) return true
        const bitrixText = (deal.bitrix_tasks || []).map((item) => item.subject).join(' ')
        return `${deal.title || ''} ${deal.deal_id} ${deal.manager_name || ''} ${bitrixText}`
          .toLocaleLowerCase('ru')
          .includes(needle)
      })
  }, [data, managerFilter, search, stageFilter, statusFilter])

  const visibleDeals = useMemo(() => {
    return [...filteredDeals]
      .filter((deal) => {
        const bitrixTask = primaryBitrixTaskOf(deal)
        if (timeView === 'all') return view === 'dashboard' || Boolean(bitrixTask) || controlTimeBucket(deal) === 'missing'
        return dealMatchesTime(deal, timeView)
      })
      .sort((a, b) => {
        const recommendationRank = view === 'rop' ? recommendationRankOf(a) - recommendationRankOf(b) : 0
        const rank = timeRank(a) - timeRank(b)
        const firstAt = primaryBitrixTaskOf(a)?.deadline
        const secondAt = primaryBitrixTaskOf(b)?.deadline
        return recommendationRank || rank || String(firstAt || '').localeCompare(String(secondAt || ''))
      })
  }, [filteredDeals, timeView, view])

  const selected = visibleDeals.find((deal) => deal.deal_id === selectedId) || null

  const selectDealExplicitly = useCallback((dealId: string) => {
    setSelectedId(dealId)
    const deal = data?.deals.find((item) => item.deal_id === dealId)
    const recommendation = deal ? neuroRopTaskOf(deal) : null
    if (user.role === 'manager' && deal?.is_own && recommendation) {
      void recordRecommendationEvent(dealId, 'viewed', 'deal_task', recommendation.id)
        .catch(() => undefined)
    }
  }, [data, user.role])

  const filteredSummary = useMemo<DealControlDashboard['summary']>(() => {
    const bitrixTasks = filteredDeals
      .map(primaryBitrixTaskOf)
      .filter((task): task is DealControlBitrixTask => Boolean(task))
    const activeBuckets = bitrixTasks.map((task) => task.time_bucket)
    const missing = filteredDeals.filter((deal) => !primaryBitrixTaskOf(deal)).length
    const overdue = activeBuckets.filter((bucket) => bucket === 'overdue').length
    const today = activeBuckets.filter((bucket) => bucket === 'today').length
    const probabilities = filteredDeals
      .map((deal) => deal.probability)
      .filter((value): value is number => value != null)
    return {
      active_deals: filteredDeals.length,
      portfolio_amount: filteredDeals.reduce((sum, deal) => sum + (Number(deal.amount) || 0), 0),
      tasks_total: bitrixTasks.length,
      tasks_today: today,
      tasks_tomorrow: activeBuckets.filter((bucket) => bucket === 'tomorrow').length,
      tasks_future: activeBuckets.filter((bucket) => bucket === 'future' || bucket === 'unscheduled').length,
      tasks_overdue: overdue,
      tasks_completed_today: bitrixTasks.filter((task) =>
        ['overdue', 'today'].includes(task.time_bucket)
        && ['local', 'bitrix'].includes(task.completion_state)
      ).length,
      tasks_missing: missing,
      tasks_plan_today: missing + overdue + today,
      average_probability: probabilities.length
        ? Math.round(probabilities.reduce((sum, value) => sum + value, 0) / probabilities.length)
        : null,
    }
  }, [filteredDeals])

  const timeCounts = useMemo(() => {
    const controlDeals = filteredDeals
    return {
      all: controlDeals.length,
      overdue: controlDeals.filter((deal) => controlTimeBucket(deal) === 'overdue').length,
      today: controlDeals.filter((deal) => ['missing', 'overdue', 'today'].includes(controlTimeBucket(deal) || '')).length,
      tomorrow: controlDeals.filter((deal) => controlTimeBucket(deal) === 'tomorrow').length,
      future: controlDeals.filter((deal) => ['future', 'unscheduled'].includes(controlTimeBucket(deal) || '')).length,
    }
  }, [filteredDeals])

  useEffect(() => {
    const visibleIds = new Set(visibleDeals.map((deal) => deal.deal_id))
    if (!visibleIds.has(selectedId)) setSelectedId(visibleDeals.find((deal) => deal.can_open)?.deal_id || '')
  }, [selectedId, visibleDeals])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), NOTICE_TOAST_MS)
    return () => window.clearTimeout(timer)
  }, [notice])

  async function sync() {
    setSyncing(true)
    setSyncStatus('Подключаемся к Bitrix…')
    setError('')
    setNotice('')
    const statusTimers = [
      window.setTimeout(() => setSyncStatus('Получаем сделки и задачи…'), 2500),
      window.setTimeout(() => setSyncStatus('Bitrix отвечает медленно — ожидайте…'), 8000),
    ]
    try {
      const response = await syncDealControl()
      setData(response)
      setSelectedId((current) => {
        if (current && response.deals.some((deal) => deal.deal_id === current && deal.can_open)) return current
        return response.deals.find((deal) => deal.can_open)?.deal_id || ''
      })
      setNotice(response.sync_message || 'Данные из Bitrix обновлены')
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason)
      setError(`Bitrix не обновлён: ${message}`)
    } finally {
      statusTimers.forEach((timer) => window.clearTimeout(timer))
      setSyncStatus('')
      setSyncing(false)
    }
  }

  async function saveScope() {
    if (user.role !== 'admin') return
    setError('')
    try {
      await saveDealControlScope({
        initial_deal_ids: splitIds(initialIds),
        manager_ids: splitIds(managerIds),
        pipeline_id: '15',
        pipeline_ids: ['15', '17', '47'],
      })
      await reload()
      setNotice('Выборка сохранена. Теперь обновите данные из Bitrix.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function saveFields(
    deal: DealControlDeal,
    patch: Partial<Pick<DealControlDeal, 'probability' | 'expected_payment_period' | 'next_control_at'>>,
  ) {
    if (!deal.can_edit) return
    setError('')
    try {
      await updateDealControlDeal(deal.deal_id, {
        probability: patch.probability === undefined ? deal.probability ?? null : patch.probability,
        expected_payment_period: patch.expected_payment_period === undefined
          ? deal.expected_payment_period ?? null
          : patch.expected_payment_period,
        next_control_at: patch.next_control_at === undefined ? deal.next_control_at ?? null : patch.next_control_at,
      })
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function toggleBitrixCompletion(deal: DealControlDeal, task: DealControlBitrixTask) {
    if (!deal.can_edit) return
    setError('')
    try {
      const completed = task.completion_state !== 'local'
      await updateDealControlBitrixTaskCompletion(
        deal.deal_id,
        task.activity_id,
        completed,
      )
      setNotice(completed ? 'Задача отмечена выполненной в приложении.' : 'Задача возвращена в работу.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function toggleChecklistItem(deal: DealControlDeal, itemId: string, completed: boolean) {
    if (!deal.can_edit) return
    setError('')
    try {
      await updateDealControlChecklistItemCompletion(deal.deal_id, itemId, completed)
      setNotice(completed ? 'Пункт чек-листа выполнен.' : 'Пункт чек-листа возвращён в работу.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function copy(text: string, label: string) {
    if (!text.trim()) {
      setNotice(`${label} пока не сформирован: нужен сохранённый анализ сделки.`)
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      setNotice(`${label} скопирован. В Bitrix его нужно перенести вручную.`)
    } catch {
      setError('Не удалось скопировать текст. Разрешите браузеру доступ к буферу обмена.')
    }
  }

  async function runAnalyzeDeal(deal: DealControlDeal, confirmPaid = false) {
    if (!deal.can_run_analysis) {
      setError('Анализ недоступен для этой сделки.')
      return
    }
    if (analysisJob && ['queued', 'running'].includes(analysisJob.status)) return
    setError('')
    setNotice('')
    setAnalyzingDealId(deal.deal_id)
    try {
      const started = await startAnalyze({
        entity_type: 'deal',
        ids: deal.deal_id,
        history_days: 60,
        include_related: true,
        include_internal: true,
        download_audio: true,
        redownload_audio: false,
        transcribe_audio: true,
        analyze: true,
        force_llm: false,
        confirm_paid: confirmPaid,
        transcript_mode: 'all',
      })
      setAnalysisJob(started)
      setNotice(`Анализ сделки #${deal.deal_id} запущен. Можно следить за этапами в карточке.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function analyzeDeal(deal: DealControlDeal) {
    if (deal.coaching.report_id) {
      setAnalysisConfirmDeal(deal)
      return
    }
    await runAnalyzeDeal(deal)
  }

  if (loading && !data) {
    return <main className="dc-shell dc-loading"><span className="dc-spinner" />Загружается контроль сделок…</main>
  }

  if (!data?.scope.configured) {
    if (user.role !== 'admin') {
      return <main className="dc-setup"><section><h1>Контроль сделок пока не настроен</h1><p>Попросите администратора настроить рабочую выборку.</p>{onLogout ? <button className="dc-button" onClick={() => void onLogout()}>Выйти</button> : null}</section></main>
    }
    return <main className="dc-setup">
      <section>
        <span className="dc-eyebrow">Первичная настройка</span>
        <h1>Контроль сделок</h1>
        <p>Сохраняем локальную выборку. Bitrix используется только для чтения.</p>
        <label>Стартовые ID сделок<textarea value={initialIds} onChange={(event) => setInitialIds(event.target.value)} /></label>
        <label>ID ответственных для новых сделок<textarea value={managerIds} onChange={(event) => setManagerIds(event.target.value)} /></label>
        <p>Рабочие воронки уже заданы: 15 «Новые клиенты - Оборудование» (все открытые этапы), 17 «Повторные клиенты - Оборудование» с этапа «Потребность выявлена», 47 «Отдел продаж 2» с этапа «Вышли на ЛПР».</p>
        <div><button className="dc-button primary" onClick={() => void saveScope()}>Сохранить выборку</button>{onExit ? <button className="dc-button" onClick={onExit}>Назад</button> : null}</div>
        {error ? <p className="dc-alert error">{error}</p> : null}
      </section>
    </main>
  }

  const copyForView = VIEW_COPY[view]

  return <main className={`dc-shell ${menuOpen ? 'menu-open' : ''}`}>
    <aside className="dc-sidebar">
      <button className="dc-menu-button" onClick={() => setMenuOpen((value) => !value)} title="Развернуть меню">
        <span>☰</span><b>Меню</b>
      </button>
      <nav>
        <button className={view === 'dashboard' ? 'active' : ''} onClick={openDashboard} title="Дашборд">
          <span>▦</span><b>Дашборд</b><small>Общий контроль сделок</small>
        </button>
        {canOpenRopView ? <button className={view === 'rop' ? 'active' : ''} onClick={openRopView} title="Контроль РОПа">
          <span>◎</span><b>Контроль РОПа</b><small>План и просрочки команды</small>
        </button> : null}
        {canOpenRopView ? <button className={view === 'daily' ? 'active' : ''} onClick={openDailyView} title="Ежедневный контроль">
          <span>▣</span><b>Ежедневный контроль</b><small>Разбор команды к планёрке</small>
        </button> : null}
        {canOpenManagerView ? <button className={view === 'manager' ? 'active' : ''} onClick={openManagerView} title={user.role === 'manager' ? 'Мои задачи' : 'Задачи менеджера'}>
          <span>✓</span><b>Мои задачи</b><small>Подготовка к касаниям</small>
        </button> : null}
      </nav>
      {onExit ? <button className="dc-exit" onClick={onExit}><span>←</span><b>К основному интерфейсу</b></button> : null}
      {onLogout ? <button className="dc-exit" onClick={() => void onLogout()}><span>⇥</span><b>Выйти</b></button> : null}
    </aside>

    <section className="dc-content">
      {view === 'daily' ? <DailyControl user={user} /> : <>
      <header className="dc-header">
        <div className="dc-header-title"><h1>{copyForView.title}</h1></div>
        <Kpis view={view} summary={filteredSummary} />
        <div className="dc-refresh">
          <span>{syncStatus || `Обновлено ${dateTime(data.generated_at)}`}</span>
          <button className="dc-button" disabled={syncing} onClick={() => void sync()}>
            {syncing ? <><span className="dc-spinner" />Обновляем Bitrix…</> : <><span>⟳</span>Обновить Bitrix</>}
          </button>
        </div>
      </header>
      <AutomaticAnalysisStatus onReportsPublished={() => { void reload() }} />

      {error ? <div className="dc-alert error">{error}</div> : null}
      {data.sync_errors.length ? <details className="dc-sync-errors"><summary>Bitrix обновлён с ограничениями: {data.sync_errors.length}</summary><ul>{data.sync_errors.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}

      <Filters
        view={view}
        showManagerFilter={view !== 'manager' || user.role !== 'manager'}
        managers={managers}
        stages={stages}
        managerFilter={managerFilter}
        stageFilter={stageFilter}
        statusFilter={statusFilter}
        search={search}
        onManager={setManagerFilter}
        onStage={setStageFilter}
        onStatus={setStatusFilter}
        onSearch={setSearch}
      />

      {analysisJob && ['queued', 'running'].includes(analysisJob.status) ? <div className="dc-analysis-global">
        <span className="dc-spinner" />
        <div><strong>Идёт анализ сделки #{analyzingDealId}</strong><small>{analysisStageDetail(analysisJob, analyzingDealId)}</small></div>
      </div> : null}

      <div
        className={`dc-workspace ${dragging ? 'dragging' : ''}`}
        ref={layoutRef}
        style={{ '--dc-left-width': `${leftWidth}%` } as CSSProperties}
      >
        <section className="dc-board">
          <TimeTabs
            view={view}
            active={timeView}
            onChange={setTimeView}
            totalDeals={filteredSummary.active_deals}
            attention={filteredSummary.tasks_missing + filteredSummary.tasks_overdue}
            today={filteredSummary.tasks_plan_today}
            tomorrow={filteredSummary.tasks_tomorrow}
            future={filteredSummary.tasks_future + filteredSummary.tasks_tomorrow}
            countForPlan={(bucket) => timeCounts[bucket as keyof typeof timeCounts] || 0}
          />
          <div className="dc-board-title">
            <div><h2>{view === 'dashboard' ? 'Обзор портфеля' : taskPlanTitle(timeView)}</h2></div>
            <span>{view === 'dashboard' ? 'Сначала критичные ›' : 'Фокус дня ›'}</span>
          </div>
          {view === 'dashboard'
            ? <DealTable deals={visibleDeals} selectedId={selected?.deal_id || ''} onSelect={selectDealExplicitly} onSaveFields={saveFields} />
            : <TaskTable view={view} deals={visibleDeals} selectedId={selected?.deal_id || ''} onSelect={selectDealExplicitly} />
          }
        </section>

        <div className="dc-resizer" onPointerDown={(event) => { event.preventDefault(); setDragging(true) }} title="Потяните, чтобы изменить ширину">⋮</div>

        <DealDetail
          view={view}
          userRole={user.role}
          deal={selected}
          onReload={reload}
          onCopy={copy}
          onNotice={(message) => { setError(''); setNotice(message) }}
          onToggleBitrixCompletion={toggleBitrixCompletion}
          onToggleChecklistItem={toggleChecklistItem}
          analysisJob={analysisJob}
          analyzingDealId={analyzingDealId}
          onAnalyze={analyzeDeal}
        />
      </div>
      </>}
    </section>

    {analysisConfirmDeal ? <div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) setAnalysisConfirmDeal(null) }}>
      <section className="dc-modal dc-analysis-confirm">
        <span>✦</span>
        <h2>Проверить новые данные?</h2>
        <p>Система обновит информацию из Bitrix и проверит новые звонки. Если появились существенные изменения, могут потребоваться платная транскрибация и новый AI-анализ.</p>
        <small>Без значимых изменений текущий анализ останется актуальным, повторный LLM-анализ не запустится.</small>
        <div><button className="dc-button" onClick={() => setAnalysisConfirmDeal(null)}>Отмена</button><button className="dc-button primary" onClick={() => { const deal = analysisConfirmDeal; setAnalysisConfirmDeal(null); void runAnalyzeDeal(deal, true) }}>Проверить и обновить</button></div>
      </section>
    </div> : null}

    {notice ? <NoticeToast message={notice} onClose={() => setNotice('')} /> : null}
  </main>
}

function Kpis({ view, summary }: { view: DealControlView; summary: DealControlDashboard['summary'] }) {
  const dashboard = view === 'dashboard'
  const values = dashboard
    ? [
        ['◇', 'Всего сделок', summary.active_deals, 'blue'],
        ['₽', 'Сумма портфеля', money(summary.portfolio_amount), 'green'],
        ['▣', 'На сегодня', summary.tasks_today, 'blue'],
        ['◷', 'Просрочено', summary.tasks_overdue, 'red'],
        ['%', 'Средняя вероятность', summary.average_probability == null ? '—' : `${summary.average_probability}%`, 'orange'],
      ]
    : [
        ['◇', view === 'rop' ? 'Всего задач на контроле' : 'Всего моих задач', summary.tasks_total, 'blue'],
        ['◷', 'Просрочено', summary.tasks_overdue, 'red'],
        ['▣', 'На сегодня', summary.tasks_today, 'blue'],
        ['▤', 'На завтра', summary.tasks_tomorrow, 'orange'],
        ['✓', 'Выполнено сегодня', `${summary.tasks_completed_today} из ${summary.tasks_plan_today}`, 'green'],
      ]
  return <section className={`dc-kpis ${dashboard ? 'dashboard' : 'tasks'}`}>
    {values.map(([icon, label, value, tone]) => <article key={String(label)} className={String(tone)}>
      <span>{icon}</span><div><small>{label}</small><strong>{value}</strong></div>
    </article>)}
  </section>
}

function Filters(props: {
  view: DealControlView
  showManagerFilter: boolean
  managers: Array<[string, string]>
  stages: string[]
  managerFilter: string
  stageFilter: string
  statusFilter: string
  search: string
  onManager: (value: string) => void
  onStage: (value: string) => void
  onStatus: (value: string) => void
  onSearch: (value: string) => void
}) {
  return <section className="dc-filters">
    {props.showManagerFilter ? <select aria-label="Менеджер" value={props.managerFilter} onChange={(event) => props.onManager(event.target.value)}>
      <option value="">{props.view === 'manager' ? 'Выберите менеджера' : 'Все менеджеры'}</option>
      {props.managers.map(([id, name]) => <option value={id} key={id}>{name}</option>)}
    </select> : null}
    {props.view === 'dashboard' ? <select value={props.stageFilter} onChange={(event) => props.onStage(event.target.value)}>
      <option value="">Все этапы</option>{props.stages.map((stage) => <option value={stage} key={stage}>{stage}</option>)}
    </select> : null}
    <select value={props.statusFilter} onChange={(event) => props.onStatus(event.target.value)}>
      <option value="">{props.view === 'dashboard' ? 'Все состояния' : 'Все статусы задач'}</option>
      <option value="pending">Не выполнено</option><option value="completed">Выполнено</option><option value="overdue">Просрочено</option>
    </select>
    <input value={props.search} onChange={(event) => props.onSearch(event.target.value)} placeholder={props.view === 'dashboard' ? 'Найти сделку, ID или задачу' : 'Найти сделку или задачу'} />
  </section>
}

function TimeTabs(props: {
  view: DealControlView
  active: TimeView
  onChange: (view: TimeView) => void
  totalDeals: number
  attention: number
  today: number
  tomorrow: number
  future: number
  countForPlan: (view: TimeView) => number
}) {
  const tabs: Array<[TimeView, string, number]> = props.view === 'dashboard'
    ? [['all', 'Все сделки', props.totalDeals], ['attention', 'Требуют внимания', props.attention], ['today', 'На сегодня', props.today], ['future', 'Будущие', props.future]]
    : [['overdue', 'Просроченные', props.countForPlan('overdue')], ['today', 'Сегодня', props.countForPlan('today')], ['tomorrow', 'Завтра', props.countForPlan('tomorrow')], ['future', 'Будущие', props.countForPlan('future')], ['all', 'Все', props.countForPlan('all')]]
  return <nav className="dc-time-tabs">
    {tabs.map(([key, label, count]) => <button className={props.active === key ? 'active' : ''} key={key} onClick={() => props.onChange(key)}>{label}<span>{count}</span></button>)}
  </nav>
}

function DealTable(props: {
  deals: DealControlDeal[]
  selectedId: string
  onSelect: (id: string) => void
  onSaveFields: (
    deal: DealControlDeal,
    patch: Partial<Pick<DealControlDeal, 'probability' | 'expected_payment_period' | 'next_control_at'>>,
  ) => Promise<void>
}) {
  const monthOptions = paymentMonthOptions()
  return <div className="dc-table-wrap">
    <div className="dc-table-scroll">
      <div className="dc-deal-columns"><span>Сделка</span><span>Дата и время контроля</span><span>Стадия</span><span>Сумма и прогноз оплаты</span></div>
      {props.deals.map((deal) => {
        const task = currentTaskOf(deal)
        const bitrixTask = primaryBitrixTaskOf(deal)
        const age = stageAge(deal.modified_at_crm)
        const controlDeadline = dateTimeParts(task?.due_at || bitrixTask?.deadline || deal.next_control_at)
        const payment = parsePaymentPeriod(deal.expected_payment_period)
        const savePayment = (week: string, month: string) => void props.onSaveFields(deal, {
          expected_payment_period: formatPaymentPeriod(week, month),
        })
        return <article className={`dc-deal-row ${task ? taskTone(task) : bitrixTask ? bitrixTaskTone(bitrixTask) : 'future'} ${props.selectedId === deal.deal_id ? 'selected' : ''}`} key={deal.deal_id} onClick={() => props.onSelect(deal.deal_id)}>
          <div className="dc-deal-main"><div className="dc-cell-card plain"><small>Сделка</small><strong>{deal.title || `Сделка #${deal.deal_id}`}</strong><p><span className="dc-deal-id">#{deal.deal_id}</span><span>♟ {deal.manager_name || 'Не назначен'}</span><span>Создана {dateOnly(deal.created_at_crm)}</span></p></div></div>
          <div className="dc-control-cell"><div className="dc-cell-card"><small>Контроль</small><time className="dc-control-deadline">{controlDeadline ? <><strong>{controlDeadline.date}</strong>{controlDeadline.time ? <span>{controlDeadline.time}</span> : null}</> : <span>Не назначен</span>}</time><ControlTimeChip task={task} bitrixTask={bitrixTask} /></div></div>
          <div className="dc-stage-cell"><div className="dc-cell-card"><small>Текущая стадия</small><strong>{deal.stage_name || 'Не указана'}</strong><p>Сделка обновлена {dateOnly(deal.modified_at_crm)}{age == null ? null : <span className={age > 30 ? 'danger' : age > 14 ? 'warn' : ''}>{age} дн.</span>}</p></div></div>
          <div className="dc-forecast-cell" onClick={(event) => event.stopPropagation()}><div className="dc-cell-card"><small>Сумма договора</small><strong>{money(deal.amount, deal.currency_id || 'RUB')}</strong><div>
            <select aria-label="Вероятность оплаты" value={deal.probability ?? ''} onChange={(event) => void props.onSaveFields(deal, { probability: event.target.value ? Number(event.target.value) : null })}><option value="">—%</option>{[0, 10, 25, 50, 60, 70, 80, 100].map((value) => <option value={value} key={value}>{value}%</option>)}</select>
            <select aria-label="Неделя оплаты" value={payment.week} onChange={(event) => savePayment(event.target.value, payment.month)}><option value="">— нед.</option>{[1, 2, 3, 4, 5].map((value) => <option value={value} key={value}>{value} нед.</option>)}</select>
            <select aria-label="Месяц оплаты" value={payment.month} onChange={(event) => savePayment(payment.week, event.target.value)}><option value="">— мес.</option>{monthOptions.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select>
          </div></div></div>
        </article>
      })}
      {!props.deals.length ? <p className="dc-empty">В выбранном разделе сделок нет.</p> : null}
    </div>
  </div>
}

function TaskTable({ view, deals, selectedId, onSelect }: { view: DealControlView; deals: DealControlDeal[]; selectedId: string; onSelect: (id: string) => void }) {
  return <div className="dc-table-wrap task-table">
    <div className="dc-table-scroll">
      <div className="dc-task-columns"><span /><span>Сделка</span><span>Этап</span><span>Текущая задача</span><span>Срок</span><span>Выполнение</span></div>
      {deals.map((deal) => {
        const bitrixTask = primaryBitrixTaskOf(deal)
        const completed = bitrixTask?.completion_state === 'local' || bitrixTask?.completion_state === 'bitrix'
        const rowTone = bitrixTask ? bitrixTaskTone(bitrixTask) : 'missing'
        const deadline = dateTimeParts(bitrixTask?.deadline)
        return <article className={`dc-task-row ${rowTone} ${selectedId === deal.deal_id ? 'selected' : ''}`} key={`${deal.deal_id}-${bitrixTask?.activity_id || 'missing'}`} onClick={() => onSelect(deal.deal_id)}>
          <span className={`dc-check ${completed ? 'checked' : ''}`}>{completed ? '✓' : ''}</span>
          <div><strong>{deal.title || `Сделка #${deal.deal_id}`}</strong><span className="dc-deal-id">#{deal.deal_id}</span></div>
          <div><span className="dc-stage-pill">{deal.stage_name || 'Не указана'}</span>{view === 'rop' ? <small>♟ {deal.manager_name}</small> : null}</div>
          <div className={`dc-task-name ${bitrixTask ? '' : 'missing'}`}><strong>{bitrixTask ? compactTaskText(bitrixTask.subject).replace(/^CRM:\s*/i, '') : 'В B24 нет открытой задачи'}</strong></div>
          <div className="dc-task-deadline-cell"><time className="dc-task-deadline">{deadline ? <><strong>{deadline.date}</strong>{deadline.time ? <span>{deadline.time}</span> : null}</> : <span>Не назначен</span>}</time></div>
          <div className="dc-task-result-cell"><ControlTimeChip task={null} bitrixTask={bitrixTask} />{view === 'rop' ? <TaskCommunicationProgress summary={deal.communications_today} /> : null}{bitrixTask?.completion_state === 'local' ? <small>В B24 ещё открыта</small> : bitrixTask?.completion_state === 'bitrix' ? <small>Подтверждено в B24</small> : null}</div>
        </article>
      })}
      {!deals.length ? <p className="dc-empty">В выбранном периоде задач нет.</p> : null}
    </div>
  </div>
}

function StatusChip({ task }: { task: DealControlTask | null }) {
  return <span className={`dc-status ${taskTone(task)}`}>{taskStatus(task)}</span>
}

function ControlTimeChip({ task, bitrixTask }: {
  task: DealControlTask | null
  bitrixTask: DealControlBitrixTask | null
}) {
  if (task) return <StatusChip task={task} />
  if (!bitrixTask) return <span className="dc-status missing">Нет задачи</span>
  const label = bitrixTask.completion_state === 'local' || bitrixTask.completion_state === 'bitrix'
    ? 'Выполнено'
    : bitrixTask.time_bucket === 'overdue'
    ? 'Просрочено'
    : bitrixTask.time_bucket === 'today'
      ? 'На сегодня'
      : bitrixTask.time_bucket === 'tomorrow'
        ? 'На завтра'
        : 'Будущее'
  return <span className={`dc-status ${bitrixTaskTone(bitrixTask)}`}>{label}</span>
}

function TaskCommunicationProgress({ summary }: { summary?: DealControlCommunicationsToday | null }) {
  const target = 3
  const available = Boolean(summary?.available)
  const completed = Math.max(0, Math.min(target, summary?.completed || 0))
  const done = available && completed >= target
  const label = available ? `Касания за сегодня: ${completed} из ${target}` : 'Касания за сегодня: данные недоступны'
  return <span className={`dc-task-communication-progress ${done ? 'done' : available ? 'partial' : 'unavailable'}`} role="img" aria-label={label} title={label}>
    {Array.from({ length: target }, (_, index) => <i className={index < completed ? 'filled' : ''} key={index} />)}
  </span>
}

function DealDetail(props: {
  view: DealControlView
  userRole: AuthUser['role']
  deal: DealControlDeal | null
  onReload: () => Promise<void>
  onCopy: (text: string, label: string) => Promise<void>
  onNotice: (message: string) => void
  onToggleBitrixCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
  analysisJob: JobState | null
  analyzingDealId: string
  onAnalyze: (deal: DealControlDeal) => Promise<void>
}) {
  const [situationModalOpen, setSituationModalOpen] = useState(false)
  const [situationContext, setSituationContext] = useState('')
  const [situationError, setSituationError] = useState('')
  const [situationJob, setSituationJob] = useState<ManagerSituationJob | null>(null)
  const pendingSituationContextRef = useRef('')
  const [savedContextForBitrix, setSavedContextForBitrix] = useState('')
  const [contextCopyFailed, setContextCopyFailed] = useState(false)
  const [contextPersistUsed, setContextPersistUsed] = useState(false)
  const [quickHelpDraft, setQuickHelpDraft] = useState('')
  const [quickHelpError, setQuickHelpError] = useState('')
  const [quickHelpJob, setQuickHelpJob] = useState<ManagerQuickHelpJob | null>(null)
  const [assistantWorkspace, setAssistantWorkspace] = useState<ManagerAssistantWorkspace | null>(null)
  const [assistantLoading, setAssistantLoading] = useState(false)
  const [assistantOpen, setAssistantOpen] = useState(false)
  // Анимируем только свежий ответ; история и повторное открытие показываем сразу.
  const [freshQuickHelpId, setFreshQuickHelpId] = useState<number | null>(null)
  const [askedByDeal, setAskedByDeal] = useState<Record<string, [boolean, boolean]>>({})
  const [scriptCopyNotice, setScriptCopyNotice] = useState('')
  const [openReviewEventId, setOpenReviewEventId] = useState('')
  const activeReportId = props.deal?.coaching.report_id
  const activeDealId = props.deal?.deal_id || ''
  // На дашборде правая панель следует роли: менеджер всегда видит свой экран,
  // admin/rop — экран РОПа. Вкладка «Мои задачи» по-прежнему открывает экран менеджера для admin.
  const managerScreen = props.userRole === 'manager' || props.view === 'manager'
  const reloadDetail = props.onReload
  const visibleRecommendation = props.deal ? neuroRopTaskOf(props.deal) : null
  const managerTelemetryEnabled = props.userRole === 'manager' && Boolean(props.deal?.is_own)

  const recordQuickHelpLifecycle = useCallback((eventType: 'shown' | 'viewed', recommendationId: number) => {
    if (!managerTelemetryEnabled || !activeDealId) return
    void recordRecommendationEvent(activeDealId, eventType, 'quick_help', recommendationId)
      .catch(() => undefined)
  }, [activeDealId, managerTelemetryEnabled])

  useEffect(() => {
    if (!managerTelemetryEnabled || !props.deal || !visibleRecommendation) return
    void recordRecommendationEvent(
      props.deal.deal_id,
      'shown',
      'deal_task',
      visibleRecommendation.id,
    ).catch(() => undefined)
  }, [managerTelemetryEnabled, props.deal, visibleRecommendation])

  useEffect(() => {
    setSituationModalOpen(false)
    setSituationContext(readDealDraft(MANAGER_SITUATION_DRAFT_PREFIX, activeDealId))
    setSituationError('')
    setSituationJob(null)
    setQuickHelpDraft(readDealDraft(MANAGER_QUICK_HELP_DRAFT_PREFIX, activeDealId))
    setQuickHelpError('')
    setQuickHelpJob(null)
    setAssistantWorkspace(null)
    setAssistantLoading(false)
    setAssistantOpen(false)
    setFreshQuickHelpId(null)
    setScriptCopyNotice('')
    setOpenReviewEventId('')
  }, [activeDealId, activeReportId])

  useEffect(() => {
    pendingSituationContextRef.current = ''
    setSavedContextForBitrix('')
    setContextCopyFailed(false)
    setContextPersistUsed(false)
  }, [activeDealId])

  useEffect(() => {
    writeDealDraft(MANAGER_SITUATION_DRAFT_PREFIX, activeDealId, situationContext)
  }, [activeDealId, situationContext])

  useEffect(() => {
    writeDealDraft(MANAGER_QUICK_HELP_DRAFT_PREFIX, activeDealId, quickHelpDraft)
  }, [activeDealId, quickHelpDraft])

  const consumeFreshQuickHelp = useCallback(() => setFreshQuickHelpId(null), [])

  const loadAssistantWorkspace = useCallback(async (open = false, silent = false) => {
    if (!activeDealId) return null
    if (!silent) setAssistantLoading(true)
    try {
      const workspace = await fetchManagerAssistantWorkspace(activeDealId)
      setAssistantWorkspace(workspace)
      if (open) setAssistantOpen(true)
      return workspace
    } catch (reason) {
      setQuickHelpError(reason instanceof Error ? reason.message : 'Не удалось загрузить помощника')
      return null
    } finally {
      if (!silent) setAssistantLoading(false)
    }
  }, [activeDealId])

  useEffect(() => {
    if (!managerScreen || !props.deal || !managerSituationIsConfirmed(managerSituationOf(props.deal))) return
    let cancelled = false
    void fetchManagerAssistantWorkspace(activeDealId)
      .then((workspace) => { if (!cancelled) setAssistantWorkspace(workspace) })
      .catch(() => { /* the main situation card already explains why help is unavailable */ })
    return () => { cancelled = true }
  }, [activeDealId, activeReportId, managerScreen, props.deal])

  const situationJobId = situationJob?.job_id
  const situationJobStatus = situationJob?.status
  const markContextSaved = useCallback((text: string) => {
    setSavedContextForBitrix(text.trim())
    setContextCopyFailed(false)
    setContextPersistUsed(false)
    setSituationModalOpen(false)
    setSituationContext('')
    setSituationError('')
  }, [])
  useEffect(() => {
    if (!managerScreen || !situationJobId || !['queued', 'running'].includes(situationJobStatus || '')) return
    let cancelled = false
    let terminalHandled = false
    const poll = async () => {
      try {
        const next = await fetchManagerSituationJob(situationJobId)
        if (cancelled || next.deal_id !== activeDealId) return
        setSituationJob(next)
        if (terminalHandled) return
        if (next.status === 'done') {
          terminalHandled = true
          markContextSaved(pendingSituationContextRef.current)
          await reloadDetail()
        } else if (next.status === 'error') {
          terminalHandled = true
          setSituationError(next.error || 'Не удалось пересобрать текущую ситуацию')
        }
      } catch (reason) {
        if (!cancelled) setSituationError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1200)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeDealId, managerScreen, markContextSaved, reloadDetail, situationJobId, situationJobStatus])

  const quickHelpJobId = quickHelpJob?.job_id
  const quickHelpJobStatus = quickHelpJob?.status
  useEffect(() => {
    if (!managerScreen || !quickHelpJobId || !['queued', 'running'].includes(quickHelpJobStatus || '')) return
    let cancelled = false
    let terminalHandled = false
    let lastSavedKey = ''
    const poll = async () => {
      try {
        const next = await fetchManagerQuickHelpJob(quickHelpJobId)
        if (cancelled || next.deal_id !== activeDealId) return
        setQuickHelpJob(next)
        const saved = next.saved_by_mode || {}
        const savedKey = `${saved.push || ''}:${saved.reanimator || ''}`
        if (savedKey !== ':' && savedKey !== lastSavedKey) {
          lastSavedKey = savedKey
          const knownId = freshQuickHelpIdFromJob(next)
          if (knownId) setFreshQuickHelpId(knownId)
          await loadAssistantWorkspace(true, true)
        }
        if (terminalHandled) return
        if (next.status === 'done') {
          terminalHandled = true
          if (!cancelled) {
            setQuickHelpError('')
            const knownId = freshQuickHelpIdFromJob(next)
            if (knownId) setFreshQuickHelpId(knownId)
            await loadAssistantWorkspace(true, Boolean(lastSavedKey))
          }
        } else if (next.status === 'error') {
          terminalHandled = true
          setQuickHelpError(next.error || 'Не удалось получить помощь тренера')
        }
      } catch (reason) {
        if (!cancelled) setQuickHelpError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1200)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeDealId, loadAssistantWorkspace, managerScreen, quickHelpJobId, quickHelpJobStatus])

  async function confirmSituation() {
    if (!props.deal) return
    setSituationError('')
    try {
      await confirmManagerSituation(props.deal.deal_id)
      setSituationContext('')
      await props.onReload()
    } catch (reason) {
      setSituationError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function refineSituation() {
    if (!props.deal) return
    const context = situationContext.trim()
    if (context.length < 1 || context.length > 4000) {
      setSituationError('Добавь контекст от 1 до 4000 символов.')
      return
    }
    if (situationJob && ['queued', 'running'].includes(situationJob.status)) return
    setSituationError('')
    pendingSituationContextRef.current = context
    try {
      const started = await startManagerSituationRefinement(props.deal.deal_id, context, true)
      setSituationJob(started)
      if (started.status === 'error') setSituationError(started.error || 'Не удалось пересобрать текущую ситуацию')
      if (started.status === 'done') {
        markContextSaved(context)
        await props.onReload()
      }
    } catch (reason) {
      setSituationError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function persistSavedContextToBitrix() {
    const text = savedContextForBitrix
    if (!text || !props.deal) return
    setContextPersistUsed(true)
    const result = await persistTextAndOpenUrl(text, bitrixDealUrl(props.deal.deal_id), {
      copy: copyTextToClipboard,
      open: (url) => Boolean(window.open(url, '_blank', 'noopener,noreferrer')),
    })
    setContextCopyFailed(!result.copied)
    if (result.copied) props.onNotice('Контекст скопирован. Вставьте его в комментарий Bitrix.')
  }

  async function copySavedContextAgain() {
    const copied = await copyTextToClipboard(savedContextForBitrix)
    setContextCopyFailed(!copied)
    if (copied) props.onNotice('Контекст скопирован. Вставьте его в комментарий Bitrix.')
  }

  async function requestQuickHelp(question: string, mode?: ManagerAssistantMode) {
    if (!props.deal) return
    const normalized = question.trim()
    if (normalized.length > 4000) {
      setQuickHelpError('Опиши вопрос от 1 до 4000 символов.')
      return
    }
    if (quickHelpJob && ['queued', 'running'].includes(quickHelpJob.status)) return
    setQuickHelpError('')
    setFreshQuickHelpId(null)
    try {
      const started = await startManagerQuickHelp(props.deal.deal_id, normalized, true, mode)
      setQuickHelpDraft('')
      setQuickHelpJob(started)
      if (started.status === 'error') setQuickHelpError(started.error || 'Не удалось получить помощь тренера')
      if (started.status === 'done') {
        const knownId = freshQuickHelpIdFromJob(started)
        if (knownId) setFreshQuickHelpId(knownId)
        const workspace = await loadAssistantWorkspace(true)
        if (!knownId) {
          const fallback = latestQuickHelpEntryId(workspace?.entries || [])
          if (fallback) setFreshQuickHelpId(fallback)
        }
      }
    } catch (reason) {
      setQuickHelpError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function openAssistant() {
    const workspace = await loadAssistantWorkspace(true)
    if (!workspace) return
    if (quickHelpJob && ['queued', 'running'].includes(quickHelpJob.status) && quickHelpAnswerReady(quickHelpJob)) return
    if (quickHelpJob && ['queued', 'running'].includes(quickHelpJob.status)) return
    if (missingCurrentModes(workspace.current_by_mode).length) {
      await requestQuickHelp('')
    }
  }

  async function completeAssistantCommunication(quickHelpId: number) {
    if (!props.deal) return
    setQuickHelpError('')
    try {
      await recordManagerCommunicationCompleted(props.deal.deal_id, quickHelpId)
      await loadAssistantWorkspace(false)
    } catch (reason) {
      setQuickHelpError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function transcribeVoice(audio: Blob) {
    if (!props.deal) throw new Error('Сделка не выбрана')
    const response = await transcribeManagerVoice(props.deal.deal_id, audio, true)
    if (!response.text?.trim()) throw new Error('Транскрибация не вернула текст. Попробуй ещё раз или введи текст вручную.')
    return response.text.trim()
  }

  function toggleAsked(index: 0 | 1) {
    if (!activeDealId) return
    setAskedByDeal((current) => {
      const previous = current[activeDealId] || [false, false]
      const next: [boolean, boolean] = [...previous]
      next[index] = !next[index]
      return { ...current, [activeDealId]: next }
    })
  }

  async function copyReviewScript() {
    const script = String(props.deal?.review?.ai_context.manager_coaching || '').trim()
    await props.onCopy(script, 'Сценарий разговора')
    setScriptCopyNotice(script.trim() ? 'Сценарий скопирован' : '')
    window.setTimeout(() => setScriptCopyNotice(''), 2500)
  }

  if (!props.deal) return <aside className="dc-detail"><p className="dc-empty">Выберите сделку в таблице.</p></aside>
  const deal = props.deal
  const coaching = deal.coaching
  const hasAnalysis = Boolean(coaching.report_id)
  const managerSituation = managerSituationOf(deal)
  const analysisBusy = Boolean(props.analysisJob && ['queued', 'running'].includes(props.analysisJob.status))
  const analysisRunning = Boolean(
    analysisBusy
    && props.analyzingDealId === deal.deal_id
  )
  const analysisButton = (
    <button
      className="dc-button dc-analyze-button"
      disabled={analysisBusy}
      onClick={() => void props.onAnalyze(deal)}
    >
      {analysisRunning
        ? <><span className="dc-spinner" />Анализируем…</>
        : <>{hasAnalysis ? 'Обновить анализ' : 'Провести анализ'}</>}
    </button>
  )
  const analysisReady = (
    <div className="dc-analysis-ready">
      <div>
        <span>✓</span>
        <div>
          <strong>AI-анализ проведён</strong>
          {coaching.analysis_created_at ? <small>{dateTime(coaching.analysis_created_at)}</small> : null}
        </div>
      </div>
      <button className="dc-button" disabled={analysisBusy} onClick={() => void props.onAnalyze(deal)}>
        {analysisRunning ? <><span className="dc-spinner" />Обновляем…</> : 'Обновить'}
      </button>
    </div>
  )
  const analysisMissing = (
    <div className="dc-analysis-ready dc-analysis-missing">
      <div>
        <span>✦</span>
        <div>
          <strong>AI-анализ не проведён</strong>
        </div>
      </div>
      {analysisButton}
    </div>
  )
  const analysisUnavailable = (
    <div className="dc-analysis-ready dc-analysis-missing">
      <div>
        <span>✦</span>
        <div>
          <strong>Анализ недоступен: сделка другого менеджера</strong>
        </div>
      </div>
    </div>
  )

  return <aside className="dc-detail">
    <header className="dc-detail-top">
      <div className="dc-detail-heading">
        <div className="dc-deal-title-row">
          <h2>Сделка #{deal.deal_id}</h2>
          <a className="dc-button primary dc-bitrix-detail-link" href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer">B24 ↗</a>
          <section className="dc-detail-stats dc-detail-stats-compact" aria-label="Основные данные сделки">
            <div className="dc-detail-stat-grow" title={`Этап: ${deal.stage_name || 'не указан'}`} aria-label={`Этап: ${deal.stage_name || 'не указан'}`}><span aria-hidden="true">◆</span><strong>{deal.stage_name || '—'}</strong></div>
            <div className="dc-detail-stat-fixed" title={`Вероятность: ${deal.probability == null ? 'не указана' : `${deal.probability}%`}`} aria-label={`Вероятность: ${deal.probability == null ? 'не указана' : `${deal.probability}%`}`}><span aria-hidden="true">◔</span><strong>{deal.probability == null ? '—' : `${deal.probability}%`}</strong></div>
            <div className="dc-detail-stat-grow" title={`Менеджер: ${deal.manager_name || 'не указан'}`} aria-label={`Менеджер: ${deal.manager_name || 'не указан'}`}><span aria-hidden="true">●</span><strong>{deal.manager_name || '—'}</strong></div>
            <div className="dc-detail-stat-fixed" title={`Сумма: ${money(deal.amount, deal.currency_id || 'RUB')}`} aria-label={`Сумма: ${money(deal.amount, deal.currency_id || 'RUB')}`}><span aria-hidden="true">₽</span><strong>{money(deal.amount, deal.currency_id || 'RUB')}</strong></div>
          </section>
        </div>
        <div className="dc-deal-compact-row">
          <p className="dc-deal-compact-title">{deal.title}</p>
          {deal.review ? <span className={`dc-daily-pill ${deal.review.status}`}>{deal.review.status_label}</span> : null}
        </div>
      {!deal.can_open ? analysisUnavailable : hasAnalysis ? analysisReady : analysisMissing}
      </div>
    </header>
    {analysisRunning && props.analysisJob ? <DealAnalysisProgress job={props.analysisJob} dealId={deal.deal_id} /> : null}

    {managerScreen ? <ManagerDealScreen
      deal={deal}
      situation={managerSituation}
      hasAnalysis={hasAnalysis}
      situationModalOpen={situationModalOpen}
      situationContext={situationContext}
      situationError={situationError}
      situationJob={situationJob}
      savedContextForBitrix={savedContextForBitrix}
      contextCopyFailed={contextCopyFailed}
      contextPersistUsed={contextPersistUsed}
      quickHelpDraft={quickHelpDraft}
      quickHelpError={quickHelpError}
      quickHelpJob={quickHelpJob}
      assistantWorkspace={assistantWorkspace}
      assistantLoading={assistantLoading}
      assistantOpen={assistantOpen}
      onOpenSituation={() => { setSituationError(''); setSituationModalOpen(true) }}
      onCloseSituation={() => setSituationModalOpen(false)}
      onSituationContext={setSituationContext}
      onConfirmSituation={() => void confirmSituation()}
      onRefineSituation={() => void refineSituation()}
      onPersistContextToBitrix={() => void persistSavedContextToBitrix()}
      onCopySavedContext={() => void copySavedContextAgain()}
      onQuickHelpDraft={setQuickHelpDraft}
      onQuickHelp={requestQuickHelp}
      onOpenAssistant={() => void openAssistant()}
      onCloseAssistant={() => { setAssistantOpen(false); consumeFreshQuickHelp() }}
      freshQuickHelpId={freshQuickHelpId}
      onFreshAnswerConsumed={consumeFreshQuickHelp}
      onRecommendationEvent={recordQuickHelpLifecycle}
      onCompleteCommunication={(quickHelpId) => void completeAssistantCommunication(quickHelpId)}
      onCopy={props.onCopy}
      onTranscribe={transcribeVoice}
      onToggleBitrixCompletion={props.onToggleBitrixCompletion}
      onToggleChecklistItem={props.onToggleChecklistItem}
    /> : <RopDealScreen
      deal={deal}
      asked={askedByDeal[deal.deal_id] || [false, false]}
      onToggleAsked={toggleAsked}
      onCopyScript={() => void copyReviewScript()}
      copyNotice={scriptCopyNotice}
      openEventId={openReviewEventId}
      onToggleEvent={(eventId) => setOpenReviewEventId((current) => current === eventId ? '' : eventId)}
    />}
  </aside>
}

type ManagerDealScreenProps = {
  deal: DealControlDeal
  situation: ManagerSituationState
  hasAnalysis: boolean
  situationModalOpen: boolean
  situationContext: string
  situationError: string
  situationJob: ManagerSituationJob | null
  savedContextForBitrix: string
  contextCopyFailed: boolean
  contextPersistUsed: boolean
  quickHelpDraft: string
  quickHelpError: string
  quickHelpJob: ManagerQuickHelpJob | null
  assistantWorkspace: ManagerAssistantWorkspace | null
  assistantLoading: boolean
  assistantOpen: boolean
  onOpenSituation: () => void
  onCloseSituation: () => void
  onSituationContext: (value: string) => void
  onConfirmSituation: () => void
  onRefineSituation: () => void
  onPersistContextToBitrix: () => void
  onCopySavedContext: () => void
  onQuickHelpDraft: (value: string) => void
  onQuickHelp: (question: string, mode?: ManagerAssistantMode) => Promise<void>
  onOpenAssistant: () => void
  onCloseAssistant: () => void
  freshQuickHelpId: number | null
  onFreshAnswerConsumed: () => void
  onRecommendationEvent: (eventType: 'shown' | 'viewed', recommendationId: number) => void
  onCompleteCommunication: (quickHelpId: number) => void
  onCopy: (text: string, label: string) => Promise<void>
  onTranscribe: (audio: Blob) => Promise<string>
  onToggleBitrixCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
}

function ManagerDealScreen(props: ManagerDealScreenProps) {
  const confirmed = managerSituationIsConfirmed(props.situation)
  return <>
    {props.hasAnalysis ? <ManagerSituationActions
      deal={props.deal}
      situation={props.situation}
      modalOpen={props.situationModalOpen}
      context={props.situationContext}
      error={props.situationError}
      job={props.situationJob}
      savedContext={props.savedContextForBitrix}
      copyFailed={props.contextCopyFailed}
      persistUsed={props.contextPersistUsed}
      onOpenModal={props.onOpenSituation}
      onCloseModal={props.onCloseSituation}
      onContext={props.onSituationContext}
      onConfirm={props.onConfirmSituation}
      onRefine={props.onRefineSituation}
      onPersistToBitrix={props.onPersistContextToBitrix}
      onCopySavedContext={props.onCopySavedContext}
      onTranscribe={props.onTranscribe}
    /> : null}
    {confirmed ? <>
      <ManagerQuickHelp
        error={props.quickHelpError}
        job={props.quickHelpJob}
        loading={props.assistantLoading}
        onOpen={props.onOpenAssistant}
      />
      <DealChecklistCard deal={props.deal} editable onToggle={props.onToggleChecklistItem} />
      <ManagerBitrixTaskCard deal={props.deal} onToggleCompletion={props.onToggleBitrixCompletion} />
      {props.assistantOpen && props.assistantWorkspace ? <ManagerAssistantModal
        deal={props.deal}
        workspace={props.assistantWorkspace}
        draft={props.quickHelpDraft}
        error={props.quickHelpError}
        job={props.quickHelpJob}
        onDraft={props.onQuickHelpDraft}
        onRequest={props.onQuickHelp}
        onClose={props.onCloseAssistant}
        onEditSituation={() => { props.onCloseAssistant(); props.onOpenSituation() }}
        onCopy={props.onCopy}
        onTranscribe={props.onTranscribe}
        onCompleteCommunication={props.onCompleteCommunication}
        onToggleChecklistItem={props.onToggleChecklistItem}
        freshEntryId={props.freshQuickHelpId}
        onFreshAnswerConsumed={props.onFreshAnswerConsumed}
        onRecommendationEvent={props.onRecommendationEvent}
      /> : null}
    </> : null}
  </>
}

function RopDealScreen({
  deal,
  asked,
  onToggleAsked,
  onCopyScript,
  copyNotice,
  openEventId,
  onToggleEvent,
}: {
  deal: DealControlDeal
  asked: [boolean, boolean]
  onToggleAsked: (index: 0 | 1) => void
  onCopyScript: () => void
  copyNotice: string
  openEventId: string
  onToggleEvent: (eventId: string) => void
}) {
  return <DealReviewCard
    deal={deal.review || null}
    asked={asked}
    onToggleAsked={onToggleAsked}
    onCopyScript={onCopyScript}
    copyNotice={copyNotice}
    openEventId={openEventId}
    onToggleEvent={onToggleEvent}
    showHeader={false}
    emptyText="Разбор сделки пока недоступен."
    contentNote="Текст письма, исходное сообщение и транскрипт здесь не показываются."
    scriptHint="Для разговора с менеджером"
  />
}

function DealChecklistCard({ deal, editable, onToggle }: {
  deal: DealControlDeal
  editable: boolean
  onToggle?: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
}) {
  const checklist = deal.checklist || { items: [], completed: 0, total: 0, progress_percent: 0 }
  const changeLabel = (kind?: string) => kind === 'carried'
    ? 'Перенесено'
    : kind === 'reopened'
      ? 'Возобновлено'
      : kind === 'new'
        ? 'Новое'
        : ''
  return <section className="dc-deal-checklist">
    <header>
      <span className="dc-deal-checklist-icon">✓</span>
      <div><h3>Чек-лист на сегодня</h3><p>Короткий план действий по этой сделке на текущий день</p></div>
      <strong>{checklist.completed} из {checklist.total}</strong>
    </header>
    <div className="dc-deal-checklist-body">
      <div className="dc-deal-checklist-progress"><span style={{ width: `${checklist.progress_percent}%` }} /><b>Выполнено {checklist.progress_percent}%</b></div>
      {checklist.items.length ? <ul>{checklist.items.map((item) => <li className={item.completed ? 'done' : ''} key={item.id}>
        <button
          type="button"
          disabled={!editable || !onToggle}
          aria-label={item.completed ? 'Вернуть пункт в работу' : 'Отметить пункт выполненным'}
          onClick={() => onToggle ? void onToggle(deal, item.id, !item.completed) : undefined}
        >{item.completed ? '✓' : ''}</button>
        <span>{item.text}{changeLabel(item.change_kind) ? <small className={`dc-checklist-origin ${item.change_kind}`}>{changeLabel(item.change_kind)}</small> : null}</span>
        <em>{item.completed ? 'Выполнено' : 'Не выполнено'}</em>
      </li>)}</ul> : <p className="dc-deal-checklist-empty">Чек-лист появится после успешного анализа сделки.</p>}
    </div>
  </section>
}

function ManagerSituationActions(props: {
  deal: DealControlDeal
  situation: ManagerSituationState
  modalOpen: boolean
  context: string
  error: string
  job: ManagerSituationJob | null
  savedContext: string
  copyFailed: boolean
  persistUsed: boolean
  onOpenModal: () => void
  onCloseModal: () => void
  onContext: (value: string) => void
  onConfirm: () => void
  onRefine: () => void
  onPersistToBitrix: () => void
  onCopySavedContext: () => void
  onTranscribe: (audio: Blob) => Promise<string>
}) {
  const confirmed = managerSituationIsConfirmed(props.situation)
  const busy = Boolean(props.job && ['queued', 'running'].includes(props.job.status))
  const needsReview = props.situation.is_current && props.situation.state === 'refined'
  const stateLabel = confirmed
    ? 'Подтверждена'
    : needsReview
      ? 'Нужно подтвердить новую формулировку'
      : 'Требует подтверждения на сегодня'

  return <>
    <section className={`dc-manager-situation ${confirmed ? 'confirmed' : 'pending'}`}>
      <header className="dc-manager-situation-head">
        <span className="dc-manager-situation-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M4 15.5h3l2.1-6 3.4 10 2.2-7H20" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" opacity=".42" />
          </svg>
        </span>
        <div className="dc-manager-situation-heading">
          <h3>Текущая ситуация</h3>
          {!confirmed ? <p>{needsReview ? 'Проверьте текст после пересборки и подтвердите его' : 'Проверьте, актуальна ли ситуация сейчас'}</p> : null}
        </div>
        <span className="dc-manager-situation-status"><i />{stateLabel}</span>
      </header>

      <div className="dc-manager-situation-body">
        <p className="dc-manager-situation-copy">{props.deal.coaching.current_situation || 'Текущая ситуация пока не сформирована.'}</p>
        {props.job ? <ManagerJobProgress job={props.job} label="Пересборка ситуации" /> : null}
        {props.error ? <p className="dc-manager-error" role="alert">{props.error}</p> : null}
      </div>

      <footer className="dc-manager-situation-actions">
        <button className="dc-button primary" disabled={busy || confirmed} onClick={props.onConfirm}>
          {confirmed ? '✓ Ситуация подтверждена' : needsReview ? 'Подтвердить ситуацию' : 'Подтвердить на сегодня'}
        </button>
        <button className="dc-button" disabled={busy} onClick={props.onOpenModal}>
          {props.situation.state === 'refined' ? 'Изменить контекст' : 'Добавить контекст'}
        </button>
      </footer>
      {props.savedContext && !busy ? (
        <div className={`dc-manager-context-persist${props.persistUsed ? ' used' : ''}`}>
          <p role={props.copyFailed ? 'alert' : undefined}>
            <strong>✓ Контекст учтён.</strong>
            {props.copyFailed
              ? ' Текст не скопировался — вставьте его в Bitrix вручную.'
              : props.persistUsed
                ? ' Вставьте скопированный текст комментарием в Bitrix.'
                : ' Чтобы он попал в следующие анализы — сохраните в Bitrix.'}
          </p>
          <div className="dc-manager-context-persist-actions">
            <button
              type="button"
              className={`dc-button${props.persistUsed ? '' : ' persist-cta'}`}
              onClick={props.onPersistToBitrix}
            >
              {props.persistUsed ? 'Открыть ещё раз →' : 'Сохранить в Bitrix →'}
            </button>
            {props.copyFailed ? (
              <button type="button" className="dc-button persist-cta" onClick={props.onCopySavedContext}>
                Скопировать текст
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
    {props.modalOpen ? createPortal(<div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) props.onCloseModal() }}>
      <section className="dc-modal dc-manager-context-modal" aria-labelledby="manager-context-title">
        <div className="dc-manager-modal-heading"><div><span className="dc-manager-modal-icon">✦</span><div><h2 id="manager-context-title">Дополнить текущую ситуацию</h2><p>Напиши, что произошло, что уже предпринимал и какой важный контекст не попал в CRM. Это пояснение менеджера, а не доказательство ответа клиента.</p></div></div><button className="dc-manager-modal-close" onClick={props.onCloseModal} aria-label="Закрыть">×</button></div>
        <div className="dc-manager-voice-field">
          <textarea
            value={props.context}
            maxLength={4000}
            onChange={(event) => props.onContext(event.target.value)}
            placeholder="Что уже пробовал менеджер, где застрял, что изменилось после последнего контакта?"
            aria-label="Контекст менеджера"
          />
          <ManagerVoiceInput dealId={props.deal.deal_id} disabled={busy} onTranscribe={props.onTranscribe} onTranscript={(text) => props.onContext(appendVoiceText(props.context, text))} />
        </div>
        <div className="dc-manager-field-footer"><small>{props.context.length}/4000</small>{props.error ? <small className="dc-manager-error">{props.error}</small> : null}</div>
        <div><button className="dc-button" disabled={busy} onClick={props.onCloseModal}>Отмена</button><button className="dc-button primary" disabled={busy || props.context.trim().length < 1 || props.context.length > 4000} onClick={props.onRefine}>{busy ? <><span className="dc-spinner" />Пересобираем…</> : 'Пересобрать ситуацию'}</button></div>
      </section>
    </div>, document.body) : null}
  </>
}

function ManagerBitrixTaskCard({ deal, onToggleCompletion }: {
  deal: DealControlDeal
  onToggleCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
}) {
  const task = primaryBitrixTaskOf(deal)
  if (task) return <BitrixTaskCard deal={deal} task={task} onToggleCompletion={onToggleCompletion} />
  return <section className={`dc-manager-bitrix-task ${task ? bitrixTaskTone(task) : 'missing'}`}>
    <div className="dc-section-head"><div><h3>Текущая задача Bitrix</h3><p>Это рабочая задача из CRM, она видна независимо от подтверждения ситуации.</p></div><span>Bitrix</span></div>
    <div className="dc-missing-task-state"><strong>В B24 нет открытой задачи</strong><p>Следующий контролируемый шаг по сделке не назначен.</p><a className="dc-button" href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer">Открыть сделку в B24 ↗</a></div>
  </section>
}

function ManagerQuickHelp(props: {
  error: string
  job: ManagerQuickHelpJob | null
  loading: boolean
  onOpen: () => void
}) {
  const busy = Boolean(props.job && ['queued', 'running'].includes(props.job.status) && !quickHelpAnswerReady(props.job))
  return <section className="dc-manager-quick-help">
    <div className="dc-section-head"><div><h3>Дожим</h3></div><span>AI</span></div>
    <button className="dc-button primary dc-manager-assistant-open" disabled={props.loading || busy} onClick={props.onOpen}>{props.loading || busy ? <><span className="dc-spinner" />Открываем…</> : 'Открыть дожим сделки'}</button>
    {props.error ? <p className="dc-manager-error" role="alert">{props.error}</p> : null}
    {props.job && ['queued', 'running'].includes(props.job.status) ? <ManagerJobProgress job={props.job} label="Подготовка пакета дожима" /> : null}
  </section>
}

function ManagerQuickHelpAnswer({ deal, entry, animate, mode, onCopy, onEdit, onComplete, onBitrix, onToggleChecklistItem, onRevealFinished }: {
  deal: DealControlDeal
  entry: ManagerQuickHelpEntry
  animate?: boolean
  mode: ManagerAssistantMode
  onCopy: (text: string, label: string) => Promise<void>
  onEdit: () => void
  onComplete: () => void
  onBitrix: () => void
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
  onRevealFinished?: () => void
}) {
  const content: ManagerQuickHelpContent = entry.content
  const [selectedStrategy, setSelectedStrategy] = useState<ManagerQuickHelpStrategy>('primary')
  const [fullScriptOpen, setFullScriptOpen] = useState(false)
  const [fullScriptMode, setFullScriptMode] = useState<ManagerFullScriptMode>('message')
  const [fullScriptJob, setFullScriptJob] = useState<ManagerFullScriptJob | null>(null)
  const [fullScriptWorkspace, setFullScriptWorkspace] = useState<ManagerFullScriptWorkspace | null>(null)
  const [fullScriptError, setFullScriptError] = useState('')
  const summaryText = content.situation_summary || 'Ситуация пока не сформирована.'
  const reveal = useQuickHelpReveal(Boolean(animate), summaryText)
  const showMessage = !reveal.animate || reveal.step !== 'summary'
  const showSecondary = !reveal.animate || reveal.step === 'secondary' || reveal.step === 'fallback' || reveal.step === 'done'
  const showFallback = !reveal.animate || reveal.step === 'fallback' || reveal.step === 'done'
  const showRest = !reveal.animate || reveal.step === 'done'

  useEffect(() => {
    if (!animate || reveal.step !== 'done') return
    onRevealFinished?.()
  }, [animate, onRevealFinished, reveal.step])

  async function openFullScript(scriptMode: ManagerFullScriptMode) {
    setFullScriptMode(scriptMode)
    setFullScriptOpen(true)
    setFullScriptError('')
    setFullScriptWorkspace(null)
    try {
      const started = await startManagerFullScript(deal.deal_id, entry.id, selectedStrategy, scriptMode, true)
      setFullScriptJob(started)
      if (started.status === 'done') {
        setFullScriptWorkspace(await fetchManagerFullScript(deal.deal_id, entry.id, selectedStrategy, scriptMode))
      }
    } catch (error) {
      setFullScriptError(error instanceof Error ? error.message : 'Не удалось открыть полный скрипт')
    }
  }

  useEffect(() => {
    if (!fullScriptOpen || !fullScriptJob || !['queued', 'running'].includes(fullScriptJob.status)) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const next = await fetchManagerFullScriptJob(fullScriptJob.job_id)
        if (cancelled) return
        setFullScriptJob(next)
        if (next.status === 'done') {
          setFullScriptWorkspace(await fetchManagerFullScript(deal.deal_id, entry.id, selectedStrategy, fullScriptMode))
        } else if (next.status === 'error') {
          setFullScriptError(next.detail || 'Не удалось подготовить полный скрипт')
        }
      } catch (error) {
        if (!cancelled) setFullScriptError(error instanceof Error ? error.message : 'Не удалось получить полный скрипт')
      }
    }, 900)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [deal.deal_id, entry.id, fullScriptJob, fullScriptMode, fullScriptOpen, selectedStrategy])

  const lever = pressureLever(content)

  return <article className={answerModeClassName(mode)} aria-busy={reveal.animate && reveal.step !== 'done'}>
    <div className="dc-manager-answer-summary">
      <span>◎</span>
      <div>
        <div className="dc-manager-answer-title">
          <h4>Понял ситуацию</h4>
          {entry.created_at ? <time dateTime={entry.created_at}>{dateTime(entry.created_at)}</time> : null}
        </div>
        <p>
          <span>{reveal.typedSummary}{reveal.animate && !reveal.summaryReady ? <i className="dc-manager-answer-caret" aria-hidden="true" /> : null}</span>
          {reveal.summaryReady && content.next_action ? <> <strong>{content.next_action}</strong></> : null}
          {reveal.summaryReady && content.expected_result ? <> <span> {content.expected_result}</span></> : null}
        </p>
      </div>
      <button className="dc-link-button" onClick={onEdit}>Изменить</button>
    </div>
    {showMessage && lever ? <ManagerPressureLeverCard lever={lever} animate={reveal.animate} /> : null}
    {showMessage || showSecondary ? <ManagerQuickHelpVariants
      content={content}
      onCopy={onCopy}
      selectedStrategy={selectedStrategy}
      onSelectedStrategy={setSelectedStrategy}
      onOpenScript={openFullScript}
      activeScriptMode={fullScriptOpen ? fullScriptMode : null}
      showScriptActions={showRest}
      showMessage={showMessage}
      showSecondary={showSecondary}
      animate={reveal.animate}
    /> : null}
    {showFallback && (content.answer_contract === 'strategy_v1' || content.answer_contract === 'strategy_v2' || content.answer_contract === 'strategy_v3') && content.fallback_action ? <div className={revealClassName('dc-manager-answer-fallback', reveal.animate)}><strong>Если не сработало</strong><span>{content.fallback_action}</span></div> : null}
    {showRest ? <div className={revealClassName('dc-manager-answer-actions', reveal.animate)}><button className="dc-button primary" onClick={onComplete}>Коммуникация выполнена</button><button className="dc-button" onClick={onBitrix}>Добавить комментарий в Bitrix24</button></div> : null}
    {fullScriptOpen ? <ManagerFullScriptModal
      deal={deal}
      selectedStrategy={selectedStrategy}
      scriptMode={fullScriptMode}
      workspace={fullScriptWorkspace}
      job={fullScriptJob}
      error={fullScriptError}
      onClose={() => setFullScriptOpen(false)}
      onCopy={onCopy}
      onToggleChecklistItem={onToggleChecklistItem}
    /> : null}
  </article>
}

function ManagerPressureLeverCard({ lever, animate = false }: { lever: { title: string; rationale: string }; animate?: boolean }) {
  const [open, setOpen] = useState(false)
  return <section className={revealClassName('dc-manager-lever', animate)}>
    <div>
      <small>Рычаг дожима</small>
      <strong>{lever.title}</strong>
    </div>
    <button type="button" className="dc-link-button" onClick={() => setOpen((value) => !value)}>{open ? 'Скрыть' : 'Подробнее'}</button>
    {open ? <p>{lever.rationale}</p> : null}
  </section>
}

function ManagerQuickHelpVariants({ content, onCopy, selectedStrategy, onSelectedStrategy, onOpenScript, activeScriptMode, showScriptActions, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpContent
  onCopy: (text: string, label: string) => Promise<void>
  selectedStrategy: ManagerQuickHelpStrategy
  onSelectedStrategy: (strategy: ManagerQuickHelpStrategy) => void
  onOpenScript: (mode: ManagerFullScriptMode) => Promise<void>
  activeScriptMode: ManagerFullScriptMode | null
  showScriptActions: boolean
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  return content.answer_contract === 'strategy_v3' || content.answer_contract === 'strategy_v2'
    ? <ManagerStrategyV2QuickHelpVariants content={content} onCopy={onCopy} selectedStrategy={selectedStrategy} onSelectedStrategy={onSelectedStrategy} onOpenScript={onOpenScript} activeScriptMode={activeScriptMode} showScriptActions={showScriptActions} showMessage={showMessage} showSecondary={showSecondary} animate={animate} />
    : content.answer_contract === 'strategy_v1'
    ? <ManagerStrategyQuickHelpVariants content={content} onCopy={onCopy} showMessage={showMessage} showSecondary={showSecondary} animate={animate} />
    : <ManagerLegacyQuickHelpVariants content={content} onCopy={onCopy} showMessage={showMessage} showSecondary={showSecondary} animate={animate} />
}

function ManagerLifehackCarousel({ lifehacks, animate = false }: { lifehacks: ManagerQuickHelpStrategyV2Content['lifehacks'] | ManagerQuickHelpStrategyV3Content['lifehacks']; animate?: boolean }) {
  const [index, setIndex] = useState(0)
  const visible = visibleLifehack(lifehacks, index)
  const item = visible?.item || null
  const total = visible?.total || 0
  const safeIndex = visible?.index || 0
  return <section className={revealClassName('dc-manager-lifehacks', animate)}>
    <div className="dc-manager-lifehacks-head">
      <h4>Лайфхаки</h4>
      {total > 1 ? <nav aria-label="Лайфхаки"><button type="button" disabled={safeIndex === 0} onClick={() => setIndex((value) => Math.max(0, value - 1))}>←</button><span>{safeIndex + 1} из {total}</span><button type="button" disabled={safeIndex >= total - 1} onClick={() => setIndex((value) => Math.min(total - 1, value + 1))}>→</button></nav> : null}
    </div>
    {item ? <article key={item.tactic_id}><strong>{item.title}</strong><p>{item.action}</p><small>{item.why_relevant}</small><em>{item.conditions}</em></article> : <p>Подходящий лайфхак не найден.</p>}
  </section>
}

function ManagerStrategyV2QuickHelpVariants({ content, onCopy, selectedStrategy, onSelectedStrategy, onOpenScript, activeScriptMode, showScriptActions, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpStrategyV2Content | ManagerQuickHelpStrategyV3Content
  onCopy: (text: string, label: string) => Promise<void>
  selectedStrategy: ManagerQuickHelpStrategy
  onSelectedStrategy: (strategy: ManagerQuickHelpStrategy) => void
  onOpenScript: (mode: ManagerFullScriptMode) => Promise<void>
  activeScriptMode: ManagerFullScriptMode | null
  showScriptActions: boolean
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  const strategies: ManagerQuickHelpStrategy[] = ['primary', 'alternative', 'pattern_break']
  const message = content.client_messages[selectedStrategy]
  const formats: Array<[ManagerFullScriptMode, string, string]> = [['call', '☎', 'Звонок'], ['message', '💬', 'Переписка'], ['email', '✉', 'Email']]
  return <div className="dc-manager-answer-v2">
    {showMessage ? <section className={revealClassName('dc-manager-answer-copy message', animate)}>
      <div><h4>Сообщение клиенту</h4><div className="dc-manager-message-tools">{showScriptActions ? <div className="dc-manager-format-icons" role="group" aria-label="Формат коммуникации">{formats.map(([mode, icon, label]) => <button key={mode} type="button" className={activeScriptMode === mode ? 'active' : ''} title={label} aria-label={label} aria-pressed={activeScriptMode === mode} onClick={() => void onOpenScript(mode)}>{icon}</button>)}</div> : null}<button className="dc-button" disabled={!message} onClick={() => void onCopy(message, 'Сообщение клиенту')}>Скопировать</button></div></div>
      <div className="dc-manager-tone-tabs labeled" role="tablist" aria-label="Вариант сообщения клиенту">{strategies.map((strategy) => <button key={strategy} type="button" role="tab" aria-selected={selectedStrategy === strategy} className={selectedStrategy === strategy ? 'active' : ''} onClick={() => onSelectedStrategy(strategy)}><span>{strategyLabel(content, strategy)}</span></button>)}</div>
      <pre>{message || 'Сообщение пока не сформировано.'}</pre>
    </section> : null}
    {showSecondary ? <ManagerLifehackCarousel lifehacks={content.lifehacks} animate={animate} /> : null}
  </div>
}

function ManagerLegacyQuickHelpVariants({ content, onCopy, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpLegacyContent
  onCopy: (text: string, label: string) => Promise<void>
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  const [clientTone, setClientTone] = useState<ManagerQuickHelpLegacyContent['recommended_client_tone']>(content.recommended_client_tone)
  const [callTone, setCallTone] = useState<ManagerQuickHelpLegacyContent['recommended_call_tone']>(content.recommended_call_tone)
  const clientTones = [
    ['calm', 'Спокойно'],
    ['confident', 'Уверенно'],
    ['direct', 'Прямо'],
  ] as const
  const callTones = [
    ['soft', 'Мягко'],
    ['business', 'Деловой'],
    ['direct', 'Прямой'],
  ] as const
  const clientMessage = content.client_messages[clientTone]
  const callScript = content.call_scripts[callTone]
  return <div className="dc-manager-answer-modules">
      {showMessage ? <section className={revealClassName('dc-manager-answer-copy message', animate)}><div><h4>Сообщение клиенту</h4><button className="dc-button" disabled={!clientMessage} onClick={() => void onCopy(clientMessage, 'Сообщение клиенту')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Тон сообщения клиенту">{clientTones.map(([tone, label]) => <button key={tone} type="button" role="tab" aria-selected={clientTone === tone} className={clientTone === tone ? 'active' : ''} onClick={() => setClientTone(tone)}><span>{label}</span>{content.recommended_client_tone === tone ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{clientMessage || 'Сообщение пока не сформировано.'}</pre></section> : null}
      {showSecondary ? <section className={revealClassName('dc-manager-answer-copy speech', animate)}><div><h4>Речевой модуль</h4><button className="dc-button" disabled={!callScript} onClick={() => void onCopy(callScript, 'Речевой модуль')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Тон речевого модуля">{callTones.map(([tone, label]) => <button key={tone} type="button" role="tab" aria-selected={callTone === tone} className={callTone === tone ? 'active' : ''} onClick={() => setCallTone(tone)}><span>{label}</span>{content.recommended_call_tone === tone ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{callScript || 'Речевой модуль пока не сформирован.'}</pre></section> : null}
    </div>
}

function ManagerStrategyQuickHelpVariants({ content, onCopy, showMessage = true, showSecondary = true, animate = false }: {
  content: ManagerQuickHelpStrategyContent
  onCopy: (text: string, label: string) => Promise<void>
  showMessage?: boolean
  showSecondary?: boolean
  animate?: boolean
}) {
  const initialMessage = content.recommended_channel === 'message' ? content.recommended_strategy : 'primary'
  const initialCall = content.recommended_channel === 'call' ? content.recommended_strategy : 'primary'
  const [messageStrategy, setMessageStrategy] = useState<ManagerQuickHelpStrategy>(initialMessage)
  const [callStrategy, setCallStrategy] = useState<ManagerQuickHelpStrategy>(initialCall)
  const strategies = [
    ['primary', 'Лучший ход'],
    ['alternative', 'Другой заход'],
    ['pattern_break', 'Смена хода'],
  ] as const
  const clientMessage = content.client_messages[messageStrategy]
  const callScript = content.call_scripts[callStrategy]
  return <div className="dc-manager-answer-modules">
    {showMessage ? <section className={revealClassName('dc-manager-answer-copy message', animate)}><div><h4>Сообщение клиенту</h4><button className="dc-button" disabled={!clientMessage} onClick={() => void onCopy(clientMessage, 'Сообщение клиенту')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Стратегия сообщения клиенту">{strategies.map(([strategy, label]) => <button key={strategy} type="button" role="tab" aria-selected={messageStrategy === strategy} className={messageStrategy === strategy ? 'active' : ''} onClick={() => setMessageStrategy(strategy)}><span>{label}</span>{content.recommended_channel === 'message' && content.recommended_strategy === strategy ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{clientMessage || 'Сообщение пока не сформировано.'}</pre></section> : null}
    {showSecondary ? <section className={revealClassName('dc-manager-answer-copy speech', animate)}><div><h4>Речевой модуль</h4><button className="dc-button" disabled={!callScript} onClick={() => void onCopy(callScript, 'Речевой модуль')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Стратегия речевого модуля">{strategies.map(([strategy, label]) => <button key={strategy} type="button" role="tab" aria-selected={callStrategy === strategy} className={callStrategy === strategy ? 'active' : ''} onClick={() => setCallStrategy(strategy)}><span>{label}</span>{content.recommended_channel === 'call' && content.recommended_strategy === strategy ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{callScript || 'Речевой модуль пока не сформирован.'}</pre></section> : null}
  </div>
}

function callBlockSpeech(block: ManagerFullScriptBlock) {
  // clarifying_question не показываем: модель не слышит живой звонок, второй пузырь только путает.
  if ('spoken_text' in block) return block.spoken_text
  return block.suggested_phrases.filter(Boolean).join('\n')
}

function CallSpeechIcon() {
  return <span className="dc-call-script-speech-icon" aria-hidden="true">
    <svg viewBox="0 0 24 24"><path d="M5 6h14v10H9l-4 3V6Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
  </span>
}

function CallScriptModal(props: {
  deal: DealControlDeal
  variantNumber: string
  discLabel: string
  script: ManagerCallScriptContent | null
  objections: ManagerObjectionHandling['items']
  copyText: string
  job: ManagerFullScriptJob | null
  error: string
  failed: boolean
  onCopy: (text: string, label: string) => Promise<void>
  onClose: () => void
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [objectionsOpen, setObjectionsOpen] = useState(false)
  const [activeObjection, setActiveObjection] = useState(0)
  const [activeStep, setActiveStep] = useState(0)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const script = props.script
  const objections = props.objections
  const selectedObjection = objections[activeObjection] || objections[0] || null

  useEffect(() => {
    setCollapsed({})
    setObjectionsOpen(false)
    setActiveObjection(0)
    setActiveStep(0)
  }, [script?.conversation_goal, script?.blocks.length])

  useEffect(() => {
    const root = scrollRef.current
    if (!root) return
    const onScroll = () => {
      const marker = root.getBoundingClientRect().top + 120
      const stages = [...root.querySelectorAll('[data-call-step]')]
      let current = 0
      stages.forEach((node, index) => {
        if (node.getBoundingClientRect().top <= marker) current = index
      })
      setActiveStep(current)
    }
    root.addEventListener('scroll', onScroll, { passive: true })
    return () => root.removeEventListener('scroll', onScroll)
  }, [script])

  function toggleStage(id: string) {
    setCollapsed((current) => ({ ...current, [id]: !current[id] }))
  }

  return <section className="dc-manager-full-script-modal dc-call-script" role="dialog" aria-modal="true" aria-labelledby="manager-full-script-title">
    <header className="dc-call-script-header">
      <div className="dc-call-script-heading">
        <h2 id="manager-full-script-title">Сценарий звонка</h2>
        <p className="dc-call-script-deal">Сделка #{props.deal.deal_id} · вариант {props.variantNumber}{props.discLabel ? <> · {props.discLabel}</> : null}</p>
      </div>
      {script?.conversation_goal ? <div className="dc-call-script-goal" title={script.conversation_goal}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M22 12h-3M12 22v-3M2 12h3"/></svg>
        <b>Цель:</b><span>{script.conversation_goal}</span>
      </div> : null}
      <div className="dc-manager-full-script-header-actions">
        <button className="dc-button dc-manager-full-script-copy" disabled={!props.copyText} onClick={() => void props.onCopy(props.copyText, 'Сценарий звонка')}>Скопировать</button>
        <button className="dc-manager-full-script-close-button" onClick={props.onClose} aria-label="Закрыть">×</button>
      </div>
    </header>
    {props.error ? <p className="dc-manager-error" role="alert">{props.error}</p> : null}
    {!script && props.failed ? <div className="dc-manager-full-script-failed"><strong>Сценарий не сформирован</strong><p>Закройте окно и попробуйте открыть скрипт ещё раз.</p><button className="dc-button" onClick={props.onClose}>Закрыть</button></div>
      : !script ? <div className="dc-manager-full-script-loading"><span className="dc-spinner" /><strong>{props.job?.detail || 'Подготавливаем сценарий разговора'}</strong><small>{props.job?.percent || 5}%</small></div>
      : <div className="dc-call-script-layout">
        <section className="dc-call-script-pane">
          <div className="dc-call-script-scroll" ref={scrollRef}>
            {script.blocks.map((block, index) => {
              const speech = callBlockSpeech(block)
              const isCollapsed = Boolean(collapsed[block.block_id])
              const extraOpen = Boolean(block.objective?.trim() || block.listen_for.length || block.transition?.trim())
              return <article className={`dc-call-script-stage ${index === activeStep ? 'active' : ''} ${isCollapsed ? 'collapsed' : ''}`} data-call-step={index} key={block.block_id}>
                <div className="dc-call-script-stage-number">{index + 1}</div>
                <div className="dc-call-script-stage-head">
                  <h3>{block.title}</h3>
                  <button type="button" aria-label={isCollapsed ? 'Развернуть шаг' : 'Свернуть шаг'} onClick={() => toggleStage(block.block_id)}>{isCollapsed ? '⌄' : '⌃'}</button>
                </div>
                {isCollapsed ? null : <div className="dc-call-script-stage-body">
                  {speech ? <div className="dc-call-script-speech"><CallSpeechIcon />{speech}</div> : null}
                  {extraOpen ? <details className="dc-call-script-more">
                    <summary>Подробнее</summary>
                    {block.objective?.trim() ? <p><b>Цель шага.</b> {block.objective}</p> : null}
                    {block.listen_for.length ? <p><b>Услышать:</b> {block.listen_for.join(' · ')}</p> : null}
                    {block.transition?.trim() ? <p><b>Переход:</b> {block.transition}</p> : null}
                  </details> : null}
                </div>}
              </article>
            })}
            {script.closing_agreement?.trim() ? <article className={`dc-call-script-stage ${activeStep === script.blocks.length ? 'active' : ''} ${collapsed.__close ? 'collapsed' : ''}`} data-call-step={script.blocks.length}>
              <div className="dc-call-script-stage-number">{script.blocks.length + 1}</div>
              <div className="dc-call-script-stage-head">
                <h3>Резюме и следующий шаг</h3>
                <button type="button" aria-label={collapsed.__close ? 'Развернуть шаг' : 'Свернуть шаг'} onClick={() => toggleStage('__close')}>{collapsed.__close ? '⌄' : '⌃'}</button>
              </div>
              {collapsed.__close ? null : <div className="dc-call-script-stage-body">
                <div className="dc-call-script-speech"><CallSpeechIcon />{script.closing_agreement}</div>
              </div>}
            </article> : null}
          </div>
          {objections.length ? <>
            <button
              type="button"
              className={`dc-call-script-objections-trigger ${objectionsOpen ? 'open' : ''}`}
              aria-expanded={objectionsOpen}
              onClick={() => setObjectionsOpen((value) => !value)}
            >
              <span aria-hidden="true">💬</span>
              <span>Возражения и отработка</span>
              <span className="count">{objections.length}</span>
              <span className="chev" aria-hidden="true">⌃</span>
            </button>
            <section className={`dc-call-script-drawer ${objectionsOpen ? 'open' : ''}`} aria-hidden={!objectionsOpen}>
              <div className="dc-call-script-drawer-head">
                <h3>Возражения и отработка</h3>
                <button type="button" onClick={() => setObjectionsOpen(false)} aria-label="Свернуть возражения">×</button>
              </div>
              <div className="dc-call-script-ob-tabs" role="tablist" aria-label="Возражения">
                {objections.map((item, index) => <button
                  key={item.objection_id || `${item.objection}-${index}`}
                  type="button"
                  role="tab"
                  aria-selected={index === activeObjection}
                  className={index === activeObjection ? 'active' : ''}
                  onClick={() => setActiveObjection(index)}
                >{compactTaskText(item.objection, 36)}</button>)}
              </div>
              {selectedObjection ? <div className="dc-call-script-ob-panel">
                <p className="dc-call-script-ob-caption">{selectedObjection.objection}</p>
                <div className="dc-call-script-ob-grid">
                  <section className="answer-card"><div className="ob-label"><span className="dot">●</span>Что сказать</div><p>{selectedObjection.manager_reply}</p></section>
                  <section className="why-card"><div className="ob-label"><span className="dot">✓</span>Зачем</div><p>{selectedObjection.next_step_goal}</p></section>
                  <section className="avoid-card"><div className="ob-label"><span className="dot">×</span>Не делать</div><p>{selectedObjection.what_not_to_do || 'Не обещать то, что нельзя подтвердить фактами сделки.'}</p></section>
                </div>
                {selectedObjection.follow_up_question?.trim() ? <details className="dc-call-script-ob-extra">
                  <summary>Уточняющий вопрос</summary>
                  <p>{selectedObjection.follow_up_question}</p>
                </details> : null}
              </div> : null}
            </section>
          </> : null}
        </section>
        <aside className="dc-call-script-aside">
          <ManagerAssistantChecklist deal={props.deal} onToggle={props.onToggleChecklistItem} variant="panel" />
        </aside>
      </div>}
  </section>
}

function ManagerFullScriptModal(props: {
  deal: DealControlDeal
  selectedStrategy: ManagerQuickHelpStrategy
  scriptMode: ManagerFullScriptMode
  workspace: ManagerFullScriptWorkspace | null
  job: ManagerFullScriptJob | null
  error: string
  onClose: () => void
  onCopy: (text: string, label: string) => Promise<void>
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
}) {
  const onClose = props.onClose
  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])
  const script = props.workspace?.script?.content
  const objections = props.workspace?.objection_handling?.items || []
  const failed = props.job?.status === 'error' || Boolean(props.error)
  const variantNumber = props.selectedStrategy === 'primary' ? '1' : props.selectedStrategy === 'alternative' ? '2' : '3'
  const title = props.scriptMode === 'call' ? 'Сценарий звонка' : props.scriptMode === 'email' ? 'Email клиенту' : 'Продолжение переписки'
  const discLabel = discProfileLabel(props.workspace?.disc_profile)
  const copyText = script ? 'script_contract' in script
    ? [
        `Цель: ${script.conversation_goal}`,
        ...script.blocks.map((block, index) => [
          `${index + 1}. ${block.title}`,
          block.objective,
          ...('spoken_text' in block
            ? [block.spoken_text]
            : block.suggested_phrases.map((phrase) => `— ${phrase}`)),
          block.listen_for.length ? `Услышать: ${block.listen_for.join(' · ')}` : '',
          `Переход: ${block.transition}`,
        ].filter(Boolean).join('\n')),
        isCallScriptContent(script)
          ? `Резюме и следующий шаг: ${script.closing_agreement}`
          : `Завершить договорённостью: ${script.closing_agreement}`,
      ].join('\n\n')
    : [
        `Тема: ${script.subject}`,
        '',
        script.greeting,
        script.context,
        ...script.questions.map((question, index) => `${index + 1}. ${question}`),
        script.value_point,
        script.call_to_action,
        script.closing,
      ].join('\n\n')
    : ''
  if (props.scriptMode === 'call') {
    return createPortal(<div className="dc-manager-full-script-layer">
      <CallScriptModal
        deal={props.deal}
        variantNumber={variantNumber}
        discLabel={discLabel}
        script={script && 'script_contract' in script && isCallScriptContent(script) ? script : null}
        objections={objections}
        copyText={copyText}
        job={props.job}
        error={props.error}
        failed={failed}
        onCopy={props.onCopy}
        onClose={props.onClose}
        onToggleChecklistItem={props.onToggleChecklistItem}
      />
    </div>, document.body)
  }
  return createPortal(<div className="dc-manager-full-script-layer">
    <section className="dc-manager-full-script-modal" role="dialog" aria-modal="true" aria-labelledby="manager-full-script-title">
      <header><div><small>Сделка #{props.deal.deal_id} · вариант {variantNumber}{props.workspace?.disc_profile ? <> · <span className="dc-manager-disc">{discLabel}</span></> : null}</small><h2 id="manager-full-script-title">{title}</h2></div><div className="dc-manager-full-script-header-actions"><button className="dc-button dc-manager-full-script-copy" disabled={!copyText} onClick={() => void props.onCopy(copyText, title)}>Скопировать</button><button className="dc-manager-full-script-close-button" onClick={props.onClose} aria-label="Закрыть">×</button></div></header>
      {props.error ? <p className="dc-manager-error" role="alert">{props.error}</p> : null}
      {!script && failed ? <div className="dc-manager-full-script-failed"><strong>Сценарий не сформирован</strong><p>Закройте окно и попробуйте открыть скрипт ещё раз.</p><button className="dc-button" onClick={props.onClose}>Закрыть</button></div> : !script ? <div className="dc-manager-full-script-loading"><span className="dc-spinner" /><strong>{props.job?.detail || 'Подготавливаем сценарий разговора'}</strong><small>{props.job?.percent || 5}%</small></div> : 'script_contract' in script ? <>
        <div className="dc-manager-full-script-grid">
          <main>
            <section className="dc-manager-full-script-goal"><small>Цель разговора</small><strong>{script.conversation_goal}</strong></section>
            <ol>{script.blocks.map((block, index) => {
              const linkedObjections = objections.filter((item) => block.relevant_objection_ids?.includes(item.objection_id))
              return <li key={block.block_id}>
              <span>{index + 1}</span><article><h3>{block.title}</h3><p>{block.objective}</p><div className="dc-manager-full-script-phrases">{'spoken_text' in block ? <><blockquote>{block.spoken_text}</blockquote>{block.clarifying_question?.trim() ? <div className="dc-manager-full-script-clarify"><small>Если нужно уточнить</small><blockquote className="clarify">{block.clarifying_question}</blockquote></div> : null}</> : block.suggested_phrases.map((phrase) => <blockquote key={phrase}>{phrase}</blockquote>)}</div>{block.listen_for.length ? <div className="dc-manager-full-script-listen"><strong>Услышать:</strong> {block.listen_for.join(' · ')}</div> : null}{linkedObjections.length ? <div className="dc-manager-block-objections"><strong>Если возникнет возражение</strong>{linkedObjections.map((item) => <div key={item.objection_id}><b>{item.objection}</b><span>{item.manager_reply}</span><small>{item.follow_up_question}</small></div>)}</div> : null}<footer><strong>Переход:</strong> {block.transition}</footer></article>
              </li>})}</ol>
            <section className="dc-manager-full-script-close"><small>{isCallScriptContent(script) ? 'Резюме разговора и следующий шаг' : 'Завершить договорённостью'}</small><strong>{script.closing_agreement}</strong></section>
          </main>
          <aside><ManagerAssistantChecklist deal={props.deal} onToggle={props.onToggleChecklistItem} defaultOpen /></aside>
        </div>
        {objections.length ? <details className="dc-manager-full-script-objections"><summary>Возражения и отработка <span>⌄</span></summary><div>{objections.map((item, index) => <article key={`${item.objection}-${index}`}><h3>{item.objection}</h3><p><strong>Как ответить:</strong> {item.manager_reply}</p><p><strong>Follow-up:</strong> {item.follow_up_question}</p><p><strong>Следующий шаг:</strong> {item.next_step_goal}</p>{item.what_not_to_do ? <p className="avoid"><strong>Чего не делать:</strong> {item.what_not_to_do}</p> : null}</article>)}</div></details> : null}
      </> : <div className="dc-manager-full-script-grid">
        <section className="dc-manager-email"><div><small>Тема</small><strong>{script.subject}</strong></div><article><p>{script.greeting}</p><p>{script.context}</p>{script.questions.length ? <ol>{script.questions.map((question) => <li key={question}>{question}</li>)}</ol> : null}<p>{script.value_point}</p><p>{script.call_to_action}</p><p>{script.closing}</p></article></section>
        <aside><ManagerAssistantChecklist deal={props.deal} onToggle={props.onToggleChecklistItem} defaultOpen /></aside>
      </div>}
    </section>
  </div>, document.body)
}

function ManagerAssistantChecklist({ deal, onToggle, defaultOpen = false, variant = 'compact' }: {
  deal: DealControlDeal
  onToggle: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
  defaultOpen?: boolean
  variant?: 'compact' | 'panel'
}) {
  const [pendingItemId, setPendingItemId] = useState<string | null>(null)
  const checklist = deal.checklist || { items: [], completed: 0, total: 0, progress_percent: 0 }
  const nextItem = checklist.items.find((item) => !item.completed)
  const preview = nextItem?.text
    || (checklist.total ? 'Все пункты на сегодня выполнены' : 'Пункты появятся после анализа сделки')
  const changeLabel = (kind?: string) => kind === 'carried'
    ? 'Перенесено'
    : kind === 'reopened'
      ? 'Возобновлено'
      : kind === 'new'
        ? 'Новое'
        : ''

  async function toggle(itemId: string, completed: boolean) {
    if (pendingItemId) return
    setPendingItemId(itemId)
    try {
      await onToggle(deal, itemId, completed)
    } finally {
      setPendingItemId(null)
    }
  }

  const items = checklist.items.length ? <ul>{checklist.items.map((item) => {
    const label = changeLabel(item.change_kind)
    return <li className={item.completed ? 'done' : ''} key={item.id}>
      <button
        type="button"
        disabled={Boolean(pendingItemId)}
        aria-busy={pendingItemId === item.id}
        aria-label={item.completed ? 'Вернуть пункт в работу' : 'Отметить пункт выполненным'}
        onClick={() => void toggle(item.id, !item.completed)}
      >{pendingItemId === item.id ? '…' : item.completed ? '✓' : ''}</button>
      <span>{item.text}{label ? <small className={`dc-checklist-origin ${item.change_kind}`}>{label}</small> : null}</span>
    </li>
  })}</ul> : <p>Чек-лист появится после успешного анализа сделки.</p>

  if (variant === 'panel') {
    return <section className="dc-call-script-check-panel">
      <h2>Чек-лист на сегодня</h2>
      <p className="dc-call-script-check-count"><b>{checklist.completed} из {checklist.total}</b> выполнено</p>
      <div className="dc-call-script-check-list">{items}</div>
    </section>
  }

  return <details className="dc-manager-assistant-checklist" open={defaultOpen || undefined}>
    <summary>
      <span className="dc-manager-assistant-checklist-icon">✓</span>
      <span className="dc-manager-assistant-checklist-heading">
        <strong>Чек-лист на сегодня</strong>
        <small>{preview}</small>
      </span>
      <b>{checklist.completed} из {checklist.total}</b>
      <i aria-hidden="true">⌄</i>
      <span className="dc-manager-assistant-checklist-progress">
        <span style={{ width: `${checklist.progress_percent}%` }} />
      </span>
    </summary>
    <div className="dc-manager-assistant-checklist-body">
      {items}
    </div>
  </details>
}

function ManagerAssistantModal(props: {
  deal: DealControlDeal
  workspace: ManagerAssistantWorkspace
  draft: string
  error: string
  job: ManagerQuickHelpJob | null
  onDraft: (value: string) => void
  onRequest: (question: string, mode?: ManagerAssistantMode) => Promise<void>
  onClose: () => void
  onEditSituation: () => void
  onCopy: (text: string, label: string) => Promise<void>
  onTranscribe: (audio: Blob) => Promise<string>
  onCompleteCommunication: (quickHelpId: number) => void
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
  freshEntryId: number | null
  onFreshAnswerConsumed: () => void
  onRecommendationEvent: (eventType: 'shown' | 'viewed', recommendationId: number) => void
}) {
  const [view, setView] = useState<'answer' | 'history' | 'context' | 'followups' | 'companion'>('answer')
  const [assistantMode, setAssistantMode] = useState<ManagerAssistantMode>('push')
  const [followups, setFollowups] = useState<ManagerFollowupsRecord | null>(null)
  const [followupsJob, setFollowupsJob] = useState<ManagerFollowupsJob | null>(null)
  const [followupsError, setFollowupsError] = useState('')
  const [companion, setCompanion] = useState<ManagerCompanionRecord | null>(null)
  const [companionLastContact, setCompanionLastContact] = useState<ManagerCompanionLastContact | null>(null)
  const [companionJob, setCompanionJob] = useState<ManagerCompanionJob | null>(null)
  const [companionError, setCompanionError] = useState('')
  const [historyOffset, setHistoryOffset] = useState(0)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const busy = Boolean(props.job && ['queued', 'running'].includes(props.job.status))
  const companionBusy = Boolean(companionJob && ['queued', 'running'].includes(companionJob.status))
  const footerBusy = view === 'companion' ? companionBusy : busy
  const companionMessage = String(companion?.content.message_text || '').trim()
  const turns = sharedTurns(props.workspace.entries)
  const latestTurn = turns.length ? turns[turns.length - 1] : null
  const safeHistoryOffset = Math.min(historyOffset, Math.max(0, turns.length - 1))
  const visibleTurnIndex = turns.length - 1 - safeHistoryOffset
  const visibleTurn = safeHistoryOffset === 0 ? latestTurn : (turns[visibleTurnIndex] || null)
  const visibleEntry = entryForTurn(visibleTurn, assistantMode)
    || (safeHistoryOffset === 0
      ? currentEntryForMode(
          props.workspace.entries,
          assistantMode,
          props.workspace.source_report_id,
          props.workspace.situation_review_id,
        )
      : null)
  const viewingLatest = safeHistoryOffset === 0
  const answerPane = assistantAnswerPane({
    hasTurn: Boolean(visibleTurn),
    busy,
    error: props.error,
  })
  const generatedAt = visibleEntry?.created_at
    || visibleTurn?.byMode.push?.created_at
    || visibleTurn?.byMode.reanimator?.created_at
    || ''
  const freshEntryId = props.job?.saved_by_mode?.[assistantMode] ?? props.freshEntryId
  const animateAnswer = Boolean(visibleEntry && shouldAnimateQuickHelpAnswer({
    entryId: visibleEntry.id,
    freshEntryId,
    viewingLatest: viewingLatest && view === 'answer',
    reducedMotion: false,
  }))
  const task = primaryBitrixTaskOf(props.deal)
  const onClose = props.onClose
  const onFreshAnswerConsumed = props.onFreshAnswerConsumed
  const onRecommendationEvent = props.onRecommendationEvent
  const visibleEntryId = visibleEntry?.id

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [onClose])

  useEffect(() => {
    if (props.freshEntryId == null) return
    if (busy || view !== 'answer' || !viewingLatest) onFreshAnswerConsumed()
  }, [busy, onFreshAnswerConsumed, props.freshEntryId, view, viewingLatest])

  useEffect(() => {
    if (view !== 'answer' || visibleEntryId == null) return
    onRecommendationEvent('shown', visibleEntryId)
    onRecommendationEvent('viewed', visibleEntryId)
  }, [onRecommendationEvent, view, visibleEntryId])

  async function send() {
    if (view === 'companion') {
      if (companionBusy || !props.draft.trim()) return
      if (!companionMessage) {
        setCompanionError('Сначала сформируйте сопроводительный текст')
        return
      }
      await generateCompanion(true, props.draft)
      props.onDraft('')
      return
    }
    if (busy || !props.draft.trim()) return
    setView('answer')
    setHistoryOffset(0)
    await props.onRequest(props.draft, assistantMode)
  }

  function switchMode(next: ManagerAssistantMode) {
    if (next === assistantMode) return
    setAssistantMode(next)
  }

  function navigateHistory(nextOffset: number) {
    const boundedOffset = Math.min(Math.max(0, nextOffset), Math.max(0, turns.length - 1))
    setHistoryOffset(boundedOffset)
  }

  function showAnswer() {
    setView('answer')
  }

  async function generateFollowups() {
    setFollowupsError('')
    try {
      const started = await startManagerFollowups(props.deal.deal_id, true)
      setFollowupsJob(started)
      if (started.status === 'done') setFollowups((await fetchManagerFollowups(props.deal.deal_id)).followups)
    } catch (error) {
      setFollowupsError(error instanceof Error ? error.message : 'Не удалось подготовить фоллоуапы')
    }
  }

  async function generateCompanion(regenerate = false, managerNote = '') {
    setCompanionError('')
    try {
      const started = await startManagerCompanion(props.deal.deal_id, true, regenerate, managerNote)
      setCompanionJob(started)
      if (started.status === 'done') {
        const payload = await fetchManagerCompanion(props.deal.deal_id)
        setCompanion(payload.companion)
        setCompanionLastContact(payload.last_contact)
        if (started.missing_reason && !payload.companion?.content.message_text) {
          setCompanionError(started.missing_reason)
        }
      }
    } catch (error) {
      setCompanionError(error instanceof Error ? error.message : 'Не удалось подготовить сопроводительный текст')
    }
  }

  useEffect(() => {
    if (view !== 'followups' || followups || followupsJob) return
    let cancelled = false
    void fetchManagerFollowups(props.deal.deal_id).then((result) => { if (!cancelled) setFollowups(result.followups) }).catch(() => undefined)
    return () => { cancelled = true }
  }, [followups, followupsJob, props.deal.deal_id, view])

  useEffect(() => {
    if (!followupsJob || !['queued', 'running'].includes(followupsJob.status)) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const next = await fetchManagerFollowupsJob(followupsJob.job_id)
        if (cancelled) return
        setFollowupsJob(next)
        if (next.status === 'done') setFollowups((await fetchManagerFollowups(props.deal.deal_id)).followups)
        if (next.status === 'error') setFollowupsError(next.detail || 'Не удалось подготовить фоллоуапы')
      } catch (error) {
        if (!cancelled) setFollowupsError(error instanceof Error ? error.message : 'Не удалось получить фоллоуапы')
      }
    }, 900)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [followupsJob, props.deal.deal_id])

  useEffect(() => {
    if (view !== 'companion') return
    let cancelled = false
    void fetchManagerCompanion(props.deal.deal_id).then((result) => {
      if (cancelled) return
      setCompanionLastContact(result.last_contact)
      if (!companionJob) setCompanion(result.companion)
    }).catch(() => undefined)
    return () => { cancelled = true }
  }, [companionJob, props.deal.deal_id, view])

  useEffect(() => {
    if (!companionJob || !['queued', 'running'].includes(companionJob.status)) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const next = await fetchManagerCompanionJob(companionJob.job_id)
        if (cancelled) return
        setCompanionJob(next)
        if (next.status === 'done') {
          const payload = await fetchManagerCompanion(props.deal.deal_id)
          if (cancelled) return
          setCompanion(payload.companion)
          setCompanionLastContact(payload.last_contact)
          if (next.missing_reason && !payload.companion?.content.message_text) setCompanionError(next.missing_reason)
        }
        if (next.status === 'error') setCompanionError(next.detail || 'Не удалось подготовить сопроводительный текст')
      } catch (error) {
        if (!cancelled) setCompanionError(error instanceof Error ? error.message : 'Не удалось получить сопроводительный текст')
      }
    }, 900)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [companionJob, props.deal.deal_id])

  function bitrixComment(entry: ManagerQuickHelpEntry) {
    const checklist = entry.content.crm_checklist.length
      ? `\nЧто зафиксировать:\n${entry.content.crm_checklist.map((item) => `• ${item}`).join('\n')}`
      : ''
    return `Запрос менеджера: ${entry.question}\nРекомендованное действие: ${entry.content.next_action}${checklist}`
  }

  async function prepareBitrixComment(entry: ManagerQuickHelpEntry) {
    await props.onCopy(bitrixComment(entry), 'Комментарий для Bitrix24')
    window.open(bitrixDealUrl(props.deal.deal_id), '_blank', 'noopener,noreferrer')
  }

  function complete(entry: ManagerQuickHelpEntry) {
    props.onCompleteCommunication(entry.id)
    props.onDraft('')
    window.setTimeout(() => inputRef.current?.focus(), 50)
  }

  return createPortal(<div className="dc-manager-assistant-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) props.onClose() }}>
    <section className={workspaceModeClassName(assistantMode)} role="dialog" aria-modal="true" aria-label="Дожим сделки">
      <aside className="dc-manager-assistant-sidebar">
        <div className="dc-manager-assistant-brand"><span>AI</span><div><strong>Дожим</strong></div></div>
        <div className="dc-manager-assistant-deal"><small>Сделка</small><strong>{props.deal.title || `Сделка #${props.deal.deal_id}`}</strong><span>#{props.deal.deal_id} · {props.deal.stage_name || 'этап не указан'}<br />{task ? compactTaskText(task.subject) : 'Нет открытой задачи'}</span><em className="dc-manager-disc">{discProfileLabel(props.workspace.disc_profile)}</em></div>
        <nav>
          <button className={view === 'answer' ? 'active' : ''} onClick={showAnswer}><span>✦</span>Дожим</button>
          <button className={view === 'history' ? 'active' : ''} onClick={() => setView('history')}><span>↻</span>История</button>
          <button className={view === 'context' ? 'active' : ''} onClick={() => setView('context')}><span>i</span>Контекст сделки</button>
          <button className={view === 'followups' ? 'active' : ''} onClick={() => setView('followups')}><span>↗</span>Фоллоуапы</button>
          <button className={view === 'companion' ? 'active' : ''} onClick={() => setView('companion')}><span>✉</span>Сопроводительный текст</button>
        </nav>
        <ManagerAssistantChecklist deal={props.deal} onToggle={props.onToggleChecklistItem} />
        <p className="dc-manager-assistant-context-status">Контекст сделки подгружен. Ответ учитывает этап, задачу и предыдущие коммуникации.</p>
      </aside>
      <main className="dc-manager-assistant-main">
        <header>
          <div className="dc-manager-mode-switch" role="tablist" aria-label="Режим работы">
            <button type="button" role="tab" aria-selected={assistantMode === 'push'} className={assistantMode === 'push' ? 'active push' : ''} onClick={() => switchMode('push')}>Дожим</button>
            <button type="button" role="tab" aria-selected={assistantMode === 'reanimator'} className={assistantMode === 'reanimator' ? 'active reanimator' : ''} onClick={() => switchMode('reanimator')}>Реаниматор</button>
          </div>
          {view === 'answer' && turns.length > 1 ? <nav className="dc-manager-request-navigation" aria-label="Навигация по рекомендациям"><button type="button" disabled={safeHistoryOffset >= turns.length - 1} onClick={() => navigateHistory(safeHistoryOffset + 1)}>← Предыдущий</button><span>{visibleTurnIndex + 1} из {turns.length}</span><button type="button" disabled={safeHistoryOffset === 0} onClick={() => navigateHistory(safeHistoryOffset - 1)}>Следующий →</button></nav> : null}
          <span className="dc-manager-disc-badge">{discProfileLabel(props.workspace.disc_profile)}</span>
          <span className="dc-manager-context-chip">Контекст учтён</span>
          <button onClick={props.onClose} aria-label="Закрыть">×</button>
        </header>
        <div className="dc-manager-assistant-content">
          <div className="dc-manager-assistant-checklist-mobile">
            <ManagerAssistantChecklist deal={props.deal} onToggle={props.onToggleChecklistItem} />
          </div>
          {view === 'answer' ? <section className="dc-manager-assistant-thread">
            {answerPane === 'empty' ? <div className="dc-manager-assistant-empty">
              <p>Рекомендация по этой сделке ещё не сформирована.</p>
              <button type="button" className="dc-button primary" onClick={() => void props.onRequest('')}>Сформировать</button>
            </div> : null}
            {answerPane === 'error' ? <div className="dc-manager-assistant-empty is-error">
              <p>{props.error}</p>
              <button type="button" className="dc-button primary" onClick={() => void props.onRequest('')}>Повторить</button>
            </div> : null}
            {visibleTurn ? <div className="dc-manager-assistant-turn" key={`${assistantMode}:${visibleTurn.key}`}>
              {visibleTurn.origin === 'auto' ? null : (
                <div className="dc-manager-assistant-user-message">
                  <div className="dc-manager-request-heading">
                    <small>Ваш запрос</small>
                    {generatedAt ? <time dateTime={generatedAt}>{dateTime(generatedAt)}</time> : null}
                  </div>
                  <p>{visibleTurn.question}</p>
                </div>
              )}
              {visibleEntry ? <ManagerQuickHelpAnswer
                deal={props.deal}
                entry={visibleEntry}
                animate={animateAnswer}
                mode={assistantMode}
                onCopy={props.onCopy}
                onEdit={props.onEditSituation}
                onComplete={() => complete(visibleEntry)}
                onBitrix={() => void prepareBitrixComment(visibleEntry)}
                onToggleChecklistItem={props.onToggleChecklistItem}
                onRevealFinished={onFreshAnswerConsumed}
              /> : <p className="dc-manager-assistant-missing-mode">В этом режиме ответа на этот запрос ещё нет.</p>}
            </div> : null}
            {busy ? <div className="dc-manager-assistant-typing" role="status"><span /><span /><span /><small>{props.job?.detail || 'Готовим рекомендацию'}</small></div> : null}
          </section> : null}
          {view === 'history' ? <section className="dc-manager-assistant-history"><h3>История работы по сделке</h3>{props.workspace.timeline.length ? <ol>{props.workspace.timeline.map((item) => <li key={item.id}><time>{dateTime(item.occurred_at)}</time><i /><p>{item.text}</p></li>)}</ol> : <p>История по сделке пока не сформирована.</p>}</section> : null}
          {view === 'context' ? <ManagerDealContextView
            deal={props.deal}
            onToggleChecklistItem={props.onToggleChecklistItem}
            dealId={props.deal.deal_id}
            context={props.workspace.context.deal_context || null}
            stage={props.workspace.context.stage}
            currentTask={props.workspace.context.current_task}
            lastCommunication={props.workspace.context.last_communication || null}
            mainRisk={props.workspace.context.main_risk}
            discProfile={props.workspace.disc_profile}
            report={props.workspace.context.report || null}
          /> : null}
          {view === 'followups' ? <section className="dc-manager-followups"><header><div><h3>Фоллоуапы / дожим</h3><p>Идеи полезных касаний по текущей ситуации и DISC-профилю клиента.</p></div><button className="dc-button primary" disabled={Boolean(followupsJob && ['queued', 'running'].includes(followupsJob.status))} onClick={() => void generateFollowups()}>{followups ? 'Открыть актуальные' : 'Сформировать'}</button></header>{followupsJob && ['queued', 'running'].includes(followupsJob.status) ? <ManagerJobProgress job={followupsJob} label="Подготовка фоллоуапов" /> : null}{followupsError ? <p className="dc-manager-error">{followupsError}</p> : null}{followups ? <><p className="summary">{followups.content.context_summary}</p><div>{followups.content.items.map((item) => <article key={item.item_id}><header><strong>{item.concern_or_scenario}</strong><span>{item.basis_status === 'confirmed' ? 'Подтверждено' : item.basis_status === 'inferred' ? 'Гипотеза' : 'Условный сценарий'}</span></header><h4>{item.idea}</h4><p>{item.why_it_may_help}</p><dl><div><dt>Формат</dt><dd>{item.followup_type}</dd></div><div><dt>Канал</dt><dd>{item.suggested_channel}</dd></div><div><dt>Когда</dt><dd>{item.timing}</dd></div><div><dt>Цель</dt><dd>{item.target_micro_conversion}</dd></div></dl><small>Основание: {item.evidence_summary}</small><em>{item.caution}</em></article>)}</div></> : <p className="empty">Фоллоуапы ещё не сформированы. Запуск создаст 3–5 идей без генерации самих материалов.</p>}</section> : null}
          {view === 'companion' ? <CompanionTextPanel
            lastContact={companionLastContact}
            companion={companion}
            job={companionJob}
            error={companionError}
            onGenerate={(regenerate) => void generateCompanion(regenerate)}
            onCopy={(text) => void props.onCopy(text, 'Сопроводительный текст')}
          /> : null}
        </div>
        <footer>
          <ManagerVoiceInput dealId={props.deal.deal_id} disabled={footerBusy} onTranscribe={props.onTranscribe} onTranscript={(text) => props.onDraft(appendVoiceText(props.draft, text))} />
          <textarea ref={inputRef} value={props.draft} maxLength={4000} onChange={(event) => props.onDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} placeholder={view === 'companion' ? 'Как переписать: короче, без даты, клиент сам наберёт…' : 'Уточните рычаг, тон или что уже пробовали...'} aria-label={view === 'companion' ? 'Уточнение сопроводительного текста' : 'Уточнение рекомендации'} />
          <button className="dc-button primary" disabled={footerBusy || !props.draft.trim() || (view === 'companion' && !companionMessage)} onClick={() => void send()}>{footerBusy ? <span className="dc-spinner" /> : view === 'companion' ? 'Переписать' : 'Отправить'}</button>
          {view === 'companion' && props.draft.trim() && !companionMessage ? <small className="dc-manager-error">Сначала сформируйте сопроводительный текст</small> : null}
          {props.error && visibleTurn && view !== 'companion' ? <small className="dc-manager-error">{props.error}</small> : null}
        </footer>
      </main>
    </section>
  </div>, document.body)
}

function contextStatusLabel(value: string) {
  const labels: Record<string, string> = {
    confirmed: 'Подтверждено',
    needs_confirmation: 'Нужно подтвердить',
    conflicted: 'Есть противоречие',
    outdated: 'Устарело',
    inferred: 'Гипотеза',
    missing: 'Не выяснено',
    active: 'Активно',
    weakened: 'Ослабло',
    resolved: 'Решено',
    partially_resolved: 'Частично решено',
    superseded: 'Заменено новым',
    unknown: 'Неизвестно',
    open: 'Открыто',
    done: 'Выполнено',
    broken: 'Нарушено',
    past: 'Прошло',
    current: 'Сейчас',
  }
  return labels[value] || value || 'Неизвестно'
}

function contextDisplay(value: unknown) {
  if (value === null || value === undefined || value === '') return 'Не указано'
  return String(value)
}

function ContextEvidence({ values }: { values: string[] }) {
  return values.length ? <details className="dc-deal-context-evidence"><summary>Основание · {values.length}</summary><ul>{values.map((value, index) => <li key={`${index}:${value}`}>{value}</li>)}</ul></details> : null
}

function ManagerDealContextView(props: {
  deal: DealControlDeal
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
  dealId: string
  context: DealContextSnapshot | null
  stage: string
  currentTask: string
  lastCommunication: { occurred_at?: string | null; text: string } | null
  mainRisk: string
  discProfile?: ManagerDiscProfile | null
  report: { report_id?: number | null; markdown_available: boolean } | null
}) {
  const [priorities, setPriorities] = useState<Record<string, 1 | 2 | 3 | null>>({})
  const [priorityBusy, setPriorityBusy] = useState('')
  const [priorityError, setPriorityError] = useState('')
  const [markdown, setMarkdown] = useState<string | null>(null)
  const [markdownOpen, setMarkdownOpen] = useState(false)
  const [markdownLoading, setMarkdownLoading] = useState(false)
  const [markdownError, setMarkdownError] = useState('')
  const context = props.context
  const checklist = <DealChecklistCard deal={props.deal} editable onToggle={props.onToggleChecklistItem} />

  useEffect(() => {
    const next: Record<string, 1 | 2 | 3 | null> = {}
    for (const lever of context?.pressure_levers || []) next[lever.lever_id] = lever.manual_priority ?? null
    setPriorities(next)
  }, [context])

  async function savePriority(leverId: string, value: string) {
    const priority = value ? Number(value) as 1 | 2 | 3 : null
    setPriorityBusy(leverId)
    setPriorityError('')
    try {
      const result = await updateDealContextLeverPriority(props.dealId, leverId, priority)
      const next: Record<string, 1 | 2 | 3 | null> = {}
      for (const item of result.priorities) next[item.lever_id] = item.priority
      setPriorities(next)
    } catch (error) {
      setPriorityError(error instanceof Error ? error.message : 'Не удалось сохранить приоритет')
    } finally {
      setPriorityBusy('')
    }
  }

  async function toggleMarkdown() {
    if (markdownOpen) {
      setMarkdownOpen(false)
      return
    }
    if (markdown) {
      setMarkdownOpen(true)
      return
    }
    const reportId = props.report?.report_id
    if (!reportId) return
    setMarkdownLoading(true)
    setMarkdownError('')
    try {
      const result = await fetchReportMarkdown(reportId)
      setMarkdown(result.markdown)
      setMarkdownOpen(true)
    } catch (error) {
      setMarkdownError(error instanceof Error ? error.message : 'Не удалось открыть Markdown-отчёт')
    } finally {
      setMarkdownLoading(false)
    }
  }

  if (!context) return <section className="dc-deal-context">
    {checklist}
    <section className="dc-manager-assistant-context-grid">
      <div><small>Этап</small><strong>{props.stage || 'Не указан'}</strong></div>
      <div><small>Текущая задача</small><strong>{props.currentTask || 'Нет открытой задачи'}</strong></div>
      <div><small>Последняя коммуникация</small><strong>{props.lastCommunication ? `${dateTime(props.lastCommunication.occurred_at)} · ${props.lastCommunication.text}` : 'Нет доступных данных'}</strong></div>
      <div><small>Главный риск</small><strong>{props.mainRisk || 'Не выделен'}</strong></div>
      <div><small>DISC клиента</small><strong>{discProfileLabel(props.discProfile)}</strong></div>
    </section>
  </section>

  const truth = context.current_truth
  const card = context.deal_card
  const bant = context.bant
  const decision = context.decision_path
  const commitments = context.commitments || []
  const journey = context.journey || []
  const timeline = journey.length ? journey : context.turning_points
  const levers = [...context.pressure_levers].sort((left, right) => {
    const leftPriority = priorities[left.lever_id] ?? left.ai_priority ?? 9
    const rightPriority = priorities[right.lever_id] ?? right.ai_priority ?? 9
    return leftPriority - rightPriority
  })
  const bantFields: Array<[string, string, { status?: string; evidence?: string[] } | undefined]> = [
    ['budget', 'Бюджет', bant?.budget],
    ['authority', 'Полномочия', bant?.authority],
    ['need', 'Потребность', bant?.need],
    ['timeframe', 'Срок', bant?.timeframe],
  ]
  const amountText = card?.amount != null && card.amount !== ''
    ? `${card.amount}${card.currency_id ? ` ${card.currency_id}` : ''}`
    : 'Не указана'
  const equipmentLabels: Record<string, string> = { labeler: 'Этикетировщик', filling_line: 'Линия розлива', block: 'Блок', unknown: 'Не определено' }
  const competitorLabels: Record<string, string> = {
    china: 'Китай / аналог',
    direct_competitor: 'Прямой конкурент',
    alternative_supplier: 'Другой поставщик',
    internal_solution: 'Внутреннее решение',
    unknown: 'Не уточнён',
    not_applicable: 'Не заявлен',
  }

  return <section className="dc-deal-context">
    {checklist}
    <header className="dc-deal-context-heading"><div><h3>Живая карта сделки</h3><p>Информационный срез последнего полного анализа. Выбранные рычаги пока не влияют на дожим и фоллоуапы.</p></div><span>Отчёт #{props.report?.report_id || '—'}</span></header>

    <section className="dc-deal-context-truth">
      <h4>Карточка сделки</h4>
      <div><small>Название</small><strong>{contextDisplay(card?.title || props.deal.title)}</strong></div>
      <div><small>Сумма</small><strong>{amountText}</strong></div>
      <div><small>Ответственный</small><strong>{contextDisplay(card?.responsible)}</strong></div>
      <div><small>Компания</small><strong>{contextDisplay(card?.company)}</strong></div>
      <div><small>Оборудование</small><strong>{contextDisplay(card?.equipment || equipmentLabels[context.solution_fit?.equipment_type || ''] || context.solution_fit?.equipment_type)}</strong></div>
      <div><small>Срок изготовления</small><strong>{contextDisplay(card?.manufacturing_days)}</strong></div>
    </section>

    <section className="dc-deal-context-truth">
      <h4>Текущая истина</h4>
      <div><small>Клиент и роль</small><strong>{truth.client_profile}</strong></div>
      <div><small>Потребность</small><strong>{truth.current_need}</strong></div>
      <div><small>Желаемый результат</small><strong>{truth.desired_outcome}</strong></div>
      <div><small>Текущий статус</small><strong>{truth.current_status}</strong></div>
      <div><small>Текущая задача</small><strong>{truth.current_task}</strong></div>
      <div><small>Контрольная точка</small><strong>{truth.next_checkpoint ? dateTime(truth.next_checkpoint) : 'Не назначена'} · {truth.next_step_owner}</strong></div>
    </section>

    {bant ? <section className="dc-deal-context-section">
      <h4>BANT</h4>
      <p className="dc-deal-context-note">Тот же расчёт, что в квалификации отчёта. Общий статус: {contextStatusLabel(bant.overall_status || 'unknown')}</p>
      <div className="dc-deal-context-cards">{bantFields.map(([key, label, item]) => <article key={key}><header><span>{label}</span><em className={item?.status || 'unknown'}>{item?.status === 'confirmed' ? 'Выяснено' : item?.status === 'missing' ? 'Не выяснено' : contextStatusLabel(item?.status || 'unknown')}</em></header><strong>{item?.evidence?.[0] || (item?.status === 'missing' ? 'Ещё не выяснено' : 'Нет формулировки')}</strong>{key === 'timeframe' && bant.timeframe ? <small>Решение: {contextDisplay(bant.timeframe.decision_timing)} · Запуск: {contextDisplay(bant.timeframe.need_or_launch_timing)}</small> : null}</article>)}</div>
      {bant.next_question ? <p className="dc-deal-context-note">Следующий вопрос: {bant.next_question}</p> : null}
    </section> : null}

    {decision ? <section className="dc-deal-context-section">
      <h4>Маршрут решения</h4>
      <div className="dc-deal-context-truth">
        <div><small>ЛПР</small><strong>{decision.decision_maker}</strong></div>
        <div><small>Кто владеет шагом</small><strong>{decision.current_step_owner}</strong></div>
        <div><small>Путь согласования</small><strong>{decision.approval_path}</strong></div>
        <div><small>Достоверность</small><strong>{contextStatusLabel(decision.basis_status)}</strong></div>
      </div>
      {decision.influencers?.length ? <p className="dc-deal-context-note">Влияют: {decision.influencers.join(', ')}</p> : null}
      <ContextEvidence values={decision.evidence || []} />
    </section> : null}

    {context.money_path ? <section className="dc-deal-context-section">
      <h4>Путь к деньгам</h4>
      <div className="dc-deal-context-truth">
        <div><small>Где застряли</small><strong>{context.money_path.stuck_point || 'Не указано'}</strong></div>
        <div><small>Кто должен сделать шаг</small><strong>{context.money_path.current_owner_of_next_step || 'unknown'}</strong></div>
        <div><small>Почему деньги под риском</small><strong>{contextDisplay(context.money_path.why_money_is_at_risk)}</strong></div>
        <div><small>Какой факт нужен</small><strong>{contextDisplay(context.money_path.next_required_fact)}</strong></div>
      </div>
      <ContextEvidence values={context.money_path.evidence || []} />
      {context.payment_blocker?.applicable ? <p className="dc-deal-context-note">Блокер оплаты: {context.payment_blocker.blocker_type || 'не указан'} · {context.payment_blocker.current_status || 'статус не указан'}</p> : null}
    </section> : null}

    {context.competitor?.applicable ? <section className="dc-deal-context-section">
      <h4>Конкурент / альтернатива</h4>
      <article><header><span>{competitorLabels[context.competitor.competitor_type || ''] || context.competitor.competitor_type}</span></header><strong>{contextDisplay(context.competitor.risk_if_not_defended)}</strong>{context.competitor.defense_points?.length ? <small>{context.competitor.defense_points.join(' · ')}</small> : null}</article>
    </section> : null}

    <section className="dc-deal-context-section"><h4>Критические факты</h4><div className="dc-deal-context-cards">{context.critical_facts.length ? context.critical_facts.map((fact) => <article key={fact.fact_id}><header><span>{fact.category}</span><em className={fact.status}>{contextStatusLabel(fact.status)}</em></header><strong>{fact.fact}</strong><small>Важность: {fact.importance} · Источник: {fact.source_type}</small><ContextEvidence values={fact.evidence} /></article>) : <p>Критические факты пока не выделены.</p>}</div></section>

    <section className="dc-deal-context-section"><h4>Рычаги сделки</h4><p className="dc-deal-context-note">Нужны минимум два рычага. Ручной приоритет сохраняется отдельно от отчёта. Один номер может быть назначен только одному рычагу.</p>{priorityError ? <small className="dc-manager-error">{priorityError}</small> : null}<div className="dc-deal-context-levers">{levers.length ? levers.map((lever) => <article key={lever.lever_id}><header><div><span>{lever.type}</span><strong>{lever.title}</strong></div><label>Приоритет<select value={priorities[lever.lever_id] ?? ''} disabled={priorityBusy === lever.lever_id} onChange={(event) => void savePriority(lever.lever_id, event.target.value)}><option value="">—</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label></header><p>{lever.fact}</p><dl><div><dt>Почему важно</dt><dd>{lever.why_important}</dd></div><div><dt>Последствие</dt><dd>{lever.business_consequence}</dd></div></dl><footer><span>{contextStatusLabel(lever.basis_status)}</span><small>Приоритет ИИ: {lever.ai_priority || '—'}</small></footer><ContextEvidence values={lever.evidence} /></article>) : <p>Рычаги пока не выделены.</p>}</div></section>

    {commitments.length ? <section className="dc-deal-context-section"><h4>Обещания сторон</h4><div className="dc-deal-context-cards">{commitments.map((item) => <article key={item.commitment_id}><header><span>{item.party}</span><em className={item.status}>{contextStatusLabel(item.status)}</em></header><strong>{item.promise}</strong><small>{item.due_at ? dateTime(item.due_at) : 'Срок не указан'} · {contextStatusLabel(item.basis_status)}</small><ContextEvidence values={item.evidence} /></article>)}</div></section> : null}

    <section className="dc-deal-context-columns">
      <div><h4>Боли и ограничения</h4>{context.pain_points.length ? context.pain_points.map((pain) => <article key={pain.pain_id}><header><strong>{pain.title}</strong><span>{contextStatusLabel(pain.status)}</span></header><p>{pain.description}</p><small>{pain.impact}</small><ContextEvidence values={pain.evidence} /></article>) : <p>Не выделены.</p>}</div>
      <div><h4>Что ещё можно узнать</h4>{context.open_questions.length ? <ul>{context.open_questions.map((item) => <li key={item}>{item}</li>)}</ul> : <p>Нет зафиксированных вопросов.</p>}</div>
    </section>

    <section className="dc-deal-context-section">
      <h4>История сделки</h4>
      <p className="dc-deal-context-note">Таймлайн от лида к текущей стадии: что узнали и чего не хватило на каждом шаге.</p>
      <ol className="dc-deal-context-timeline dc-deal-context-timeline-scroll">{timeline.length ? timeline.map((point) => {
        const journeyPoint = 'entry_id' in point ? point : null
        const turningPoint = 'turning_point_id' in point ? point : null
        const key = journeyPoint?.entry_id || turningPoint?.turning_point_id || point.title
        return <li key={key}>
          <time>{point.occurred_at ? dateTime(point.occurred_at) : 'Дата не указана'}</time>
          <div>
            <header><strong>{point.title}</strong><span>{contextStatusLabel(point.status)}</span></header>
            <p>{journeyPoint?.what_happened || turningPoint?.what_happened}</p>
            {journeyPoint ? <small>Узнали: {(journeyPoint.learned || []).join('; ') || 'нет данных'}. Не хватило: {(journeyPoint.missing || []).join('; ') || 'нет данных'}.</small> : <small>{turningPoint?.impact}</small>}
            {turningPoint ? <ContextEvidence values={turningPoint.evidence} /> : null}
          </div>
        </li>
      }) : <li><div><p>История пока не выделена. Появится после следующего полного анализа.</p></div></li>}</ol>
    </section>

    {context.source_conflicts.length ? <section className="dc-deal-context-section warning"><h4>Противоречия источников</h4>{context.source_conflicts.map((conflict, index) => <article key={`${index}:${conflict.description}`}><strong>{conflict.description}</strong><p>{conflict.sources.join(' · ')}</p><small>Проверить: {conflict.next_check}</small></article>)}</section> : null}

    <section className="dc-analysis-material dc-manager-markdown dc-deal-context-markdown"><button className="dc-analysis-material-link" disabled={markdownLoading || !props.report?.report_id || props.report.markdown_available === false} onClick={() => void toggleMarkdown()}>{markdownLoading ? 'Открываем полный отчёт…' : markdownOpen ? 'Скрыть полный Markdown-отчёт' : 'Открыть полный Markdown-отчёт'}</button>{markdownError ? <small className="dc-manager-error">{markdownError}</small> : null}{markdownOpen && markdown ? <pre>{markdown}</pre> : null}</section>
  </section>
}

function companionContactLabel(contact: ManagerCompanionLastContact | null) {
  if (!contact) return 'Нет данных'
  const channel = contact.channel === 'call' ? 'звонок' : contact.channel === 'email' ? 'письмо' : contact.channel === 'message' ? 'сообщение' : contact.channel || 'касание'
  const when = dateTime(contact.occurred_at)
  const seconds = Math.max(0, Math.round(Number(contact.duration_seconds || 0)))
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  const duration = contact.channel === 'call' && seconds
    ? ` · ${minutes} мин ${String(rest).padStart(2, '0')} сек`
    : ''
  return [when, channel].filter(Boolean).join(' · ') + duration
}

function CompanionTextPanel({
  lastContact,
  companion,
  job,
  error,
  onGenerate,
  onCopy,
}: {
  lastContact: ManagerCompanionLastContact | null
  companion: ManagerCompanionRecord | null
  job: ManagerCompanionJob | null
  error: string
  onGenerate: (regenerate: boolean) => void
  onCopy: (text: string) => void
}) {
  const running = Boolean(job && ['queued', 'running'].includes(job.status))
  const message = String(companion?.content.message_text || '').trim()
  const understood = companion?.content.understood || []
  const missing = !message && (error === 'Нет данных' || companion?.content.insufficient_reason)
  return (
    <section className="dc-manager-companion">
      <header>
        <div>
          <h3>Сопроводительный текст</h3>
          <p>После разговора система сначала читает Bitrix по этой сделке, затем решает, нужен ли анализ, и готовит короткое сообщение клиенту.</p>
        </div>
        <button className="dc-button primary" disabled={running} onClick={() => onGenerate(Boolean(message))}>
          {message ? 'Сформировать снова' : 'Сформировать сопроводительный текст'}
        </button>
      </header>
      <p className="summary">Последний контакт: {companionContactLabel(lastContact)}</p>
      {running ? <ManagerJobProgress job={job || { status: 'running', detail: 'Обновляем данные из Bitrix', percent: 12 }} label="Сопроводительный текст" /> : null}
      {error && error !== 'Нет данных' ? <p className="dc-manager-error">{error}</p> : null}
      {message ? (
        <>
          {understood.length ? <div className="understood"><small>Что система поняла</small><ul>{understood.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
          <pre>{message}</pre>
          <button type="button" className="dc-button" onClick={() => onCopy(message)}>Скопировать</button>
        </>
      ) : !running ? <p className="empty">{missing ? 'Нет данных' : 'Нажмите кнопку: сначала обновим Bitrix по этой сделке, потом решим, нужен ли анализ. Пока ничего не генерируется.'}</p> : null}
    </section>
  )
}

function ManagerJobProgress({ job, label }: { job: Pick<ManagerSituationJob, 'status' | 'detail' | 'percent' | 'error'>; label: string }) {
  const error = job.status === 'error'
  const done = job.status === 'done'
  return <div className={`dc-manager-job ${error ? 'error' : done ? 'done' : ''}`} role="status" aria-live="polite">
    <div><strong>{error ? `${label}: ошибка` : done ? `${label}: готово` : job.detail || `${label}…`}</strong><b>{Math.max(0, Math.min(100, job.percent || 0))}%</b></div>
    <span><i style={{ width: `${Math.max(0, Math.min(100, job.percent || 0))}%` }} /></span>
    {error ? <small>{job.error || 'Повтори действие после проверки ошибки.'}</small> : null}
  </div>
}

function ManagerVoiceInput({ dealId, disabled, onTranscribe, onTranscript }: {
  dealId: string
  disabled?: boolean
  onTranscribe: (audio: Blob) => Promise<string>
  onTranscript: (text: string) => void
}) {
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<BlobPart[]>([])
  const cancelRef = useRef(false)
  const mountedRef = useRef(true)
  const timerRef = useRef<number | null>(null)
  const intervalRef = useRef<number | null>(null)
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState('')

  function clearTimers() {
    if (timerRef.current != null) window.clearTimeout(timerRef.current)
    if (intervalRef.current != null) window.clearInterval(intervalRef.current)
    timerRef.current = null
    intervalRef.current = null
  }

  function releaseMedia() {
    clearTimers()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    recorderRef.current = null
    chunksRef.current = []
    if (mountedRef.current) {
      setRecording(false)
      setSeconds(0)
    }
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      cancelRef.current = true
      clearTimers()
      if (recorderRef.current && recorderRef.current.state !== 'inactive') recorderRef.current.stop()
      streamRef.current?.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      recorderRef.current = null
      chunksRef.current = []
    }
  }, [dealId])

  function reportError(message: string) {
    setError(message)
  }

  async function startRecording() {
    setError('')
    if (typeof window === 'undefined' || !('MediaRecorder' in window)) {
      reportError('Этот браузер не поддерживает запись голосовых сообщений. Введи текст вручную.')
      return
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      reportError('Браузер не дал доступ к микрофону. Проверь HTTPS/localhost и разрешение микрофона.')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!mountedRef.current) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      const mimeCandidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']
      const mimeType = mimeCandidates.find((value) => typeof MediaRecorder.isTypeSupported !== 'function' || MediaRecorder.isTypeSupported(value)) || ''
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      streamRef.current = stream
      recorderRef.current = recorder
      chunksRef.current = []
      cancelRef.current = false
      recorder.ondataavailable = (event) => {
        if (event.data.size) chunksRef.current.push(event.data)
      }
      recorder.onstop = async () => {
        const cancelled = cancelRef.current
        const chunks = chunksRef.current
        const type = recorder.mimeType || mimeType || 'audio/webm'
        releaseMedia()
        if (cancelled || !chunks.length || !mountedRef.current) return
        setTranscribing(true)
        try {
          const transcript = await onTranscribe(new Blob(chunks, { type }))
          if (mountedRef.current) onTranscript(transcript)
        } catch (reason) {
          if (mountedRef.current) reportError(reason instanceof Error ? reason.message : 'Не удалось распознать запись. Попробуй ещё раз или введи текст вручную.')
        } finally {
          if (mountedRef.current) setTranscribing(false)
        }
      }
      recorder.start(250)
      setRecording(true)
      setSeconds(0)
      intervalRef.current = window.setInterval(() => setSeconds((value) => Math.min(300, value + 1)), 1000)
      timerRef.current = window.setTimeout(() => stopRecording(), 300_000)
    } catch (reason) {
      streamRef.current?.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      reportError(reason instanceof DOMException && reason.name === 'NotAllowedError'
        ? 'Доступ к микрофону запрещён. Разреши микрофон в настройках браузера или введи текст вручную.'
        : 'Не удалось начать запись. Проверь микрофон и разрешение браузера.')
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') return
    cancelRef.current = false
    recorder.stop()
  }

  function cancelRecording() {
    cancelRef.current = true
    const recorder = recorderRef.current
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    else releaseMedia()
  }

  return <div className="dc-manager-voice-control">
    {recording
      ? <><button type="button" className="dc-manager-voice-button recording" onClick={stopRecording} disabled={disabled}>■ Остановить {String(Math.floor(seconds / 60)).padStart(2, '0')}:{String(seconds % 60).padStart(2, '0')}</button><button type="button" className="dc-manager-voice-cancel" onClick={cancelRecording} disabled={disabled}>Отмена</button></>
      : <button type="button" className="dc-manager-voice-button" onClick={() => void startRecording()} disabled={disabled || transcribing} title="Записать голосом">{transcribing ? <><span className="dc-spinner" />Распознаём…</> : '🎙 Говорить'}</button>}
    {error ? <small className="dc-manager-voice-error" role="alert">{error}</small> : null}
  </div>
}

function entityAnalysisProgress(job: JobState, dealId: string) {
  return job.entity_progress?.[`deal:${dealId}`] || Object.values(job.entity_progress || {}).find(
    (item) => item.entity_type === 'deal' && String(item.entity_id) === dealId,
  )
}

function analysisStageDetail(job: JobState, dealId: string) {
  const progress = entityAnalysisProgress(job, dealId)
  const stage = progress?.stage || (job.status === 'done' ? 'done' : job.status === 'error' ? 'error' : 'queued')
  return progress?.detail || ANALYSIS_STAGE_LABELS[stage] || 'Подготавливаем данные сделки'
}

function analysisPercent(stage: string, progress: ReturnType<typeof entityAnalysisProgress>, isDone: boolean) {
  if (isDone) return 100
  const base = ANALYSIS_STAGE_PROGRESS[stage] ?? 10
  const ranges: Record<string, number> = { audio_download: 49, transcription: 71 }
  const end = ranges[stage]
  const current = Number(progress?.current)
  const total = Number(progress?.total)
  if (!end || !Number.isFinite(current) || !Number.isFinite(total) || total <= 0) return base
  return Math.min(end, Math.round(base + ((Math.max(0, current) / total) * (end - base))))
}

function DealAnalysisProgress({ job, dealId }: { job: JobState; dealId: string }) {
  const progress = entityAnalysisProgress(job, dealId)
  const stage = progress?.stage || (job.status === 'done' ? 'done' : job.status === 'error' ? 'error' : 'queued')
  const isError = job.status === 'error' || progress?.status === 'error'
  const isDone = job.status === 'done' || progress?.status === 'done' || progress?.status === 'skipped'
  const percent = analysisPercent(stage, progress, isDone)
  const counter = progress?.total && progress.total > 0
    ? `${stage === 'transcription' ? 'Звонок' : 'Файл'} ${progress.current || 0} из ${progress.total}`
    : ''
  const steps = [
    ['crm_context', 'История CRM', 15],
    ['transcription', 'Звонки и транскрипты', 50],
    ['llm_analysis', 'LLM-анализ', 72],
    ['validation', 'Проверка', 86],
    ['done', 'Готово', 100],
  ] as const
  return <section className={`dc-analysis-progress ${isError ? 'error' : isDone ? 'done' : 'running'}`} role="status" aria-live="polite">
    <div className="dc-analysis-progress-head">
      <div>{!isDone && !isError ? <span className="dc-spinner" /> : <span>{isDone ? '✓' : '!'}</span>}<strong>{ANALYSIS_STAGE_LABELS[stage] || 'Выполняется анализ сделки'}</strong></div>
      <b>{percent}%</b>
    </div>
    <div className="dc-analysis-progress-bar"><span style={{ width: `${percent}%` }} /></div>
    <ol>
      {steps.map(([key, label, threshold]) => <li className={isError && percent < threshold ? '' : percent >= threshold ? 'done' : percent + 20 >= threshold ? 'active' : ''} key={key}><i>{percent >= threshold ? '✓' : ''}</i><span>{label}</span></li>)}
    </ol>
    <p>{isError ? progress?.error || job.error || 'Проверьте сообщение об ошибке и повторите запуск.' : <>{counter ? <strong>{counter}. </strong> : null}{analysisStageDetail(job, dealId)}</>}</p>
    {!isDone && !isError && progress?.updated_at ? <small>Последнее обновление: {dateTime(progress.updated_at)}</small> : null}
    {stage === 'skipped' ? <small>Платный полный вызов не потребовался: значимых новых клиентских данных не обнаружено.</small> : null}
  </section>
}

function BitrixTaskCard({ deal, task, onToggleCompletion }: {
  deal: DealControlDeal
  task: DealControlBitrixTask
  onToggleCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const description = task.description?.trim() || ''
  const hasDescription = Boolean(description)
  // Короткие тексты не режем — кнопка «Показать полностью» только когда реально нужно.
  const canCollapse = description.length > 140
  const deadline = bitrixTaskDeadline(task)
  const title = bitrixTaskDisplayTitle(deal, task)
  const completed = task.completion_state !== 'open'
  const openUrl = bitrixTaskUrl(task)

  useEffect(() => {
    setExpanded(false)
  }, [task.activity_id])

  return <section className={`dc-bitrix-task-card ${completed ? 'done' : task.time_bucket}`} aria-label="Текущая задача Bitrix">
    <header className="dc-bitrix-task-top">
      <div className="dc-bitrix-task-mainline">
        <span className="dc-bitrix-task-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <rect x="5" y="4" width="14" height="16" rx="2.5" stroke="currentColor" strokeWidth="1.8" />
            <path d="M9 4.5V3.8C9 2.8 9.8 2 10.8 2h2.4C14.2 2 15 2.8 15 3.8v.7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            <path d="M8.5 10.8l2.1 2.1 4.7-4.7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <div className="dc-bitrix-task-heading">
          <h3 className="dc-bitrix-task-title"><span>Задача:</span> {title || 'Без названия'}</h3>
          <p className="dc-bitrix-task-deal">{deal.title || `Сделка #${deal.deal_id}`}</p>
        </div>
      </div>
      <div className="dc-bitrix-task-deadline">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="8.5" stroke="currentColor" strokeWidth="1.8" />
          <path d="M12 7.5V12l3 2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        <span>{deadline.label}</span>
        {deadline.value ? <strong>{deadline.value}</strong> : null}
      </div>
    </header>

    <div className="dc-bitrix-task-body">
      {hasDescription
        ? <p className={`dc-bitrix-task-description ${canCollapse && !expanded ? 'collapsed' : ''}`}>{description}</p>
        : <p className="dc-bitrix-task-description muted">Описание задачи в Bitrix не заполнено</p>}
      {hasDescription && canCollapse ? <button
        className={`dc-bitrix-task-description-toggle ${expanded ? 'open' : ''}`}
        type="button"
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? 'Скрыть' : 'Показать полностью'}
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path d="M5 7.5l5 5 5-5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button> : null}
    </div>

    <footer className="dc-bitrix-task-actions">
      {task.completion_state === 'bitrix'
        ? <button className="dc-bitrix-task-btn primary" type="button" disabled>Выполнено в B24</button>
        : <button
            className={`dc-bitrix-task-btn ${task.completion_state === 'local' ? 'secondary' : 'primary'}`}
            type="button"
            onClick={() => void onToggleCompletion(deal, task)}
          >
            {task.completion_state === 'local' ? 'Вернуть в работу' : 'Отметить выполненной'}
          </button>}
      {openUrl
        ? <a className="dc-bitrix-task-btn secondary" href={openUrl} target="_blank" rel="noreferrer">Открыть в Bitrix24 ↗</a>
        : <button className="dc-bitrix-task-btn secondary" type="button" disabled title="Bitrix не передал ID связанной задачи">Открыть в Bitrix24 ↗</button>}
    </footer>
  </section>
}
