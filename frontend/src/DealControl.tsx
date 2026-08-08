import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  confirmDealControlTaskCrmMatch,
  confirmManagerSituation,
  createDealControlTask,
  fetchDealControl,
  fetchDealTaskGuidanceJob,
  fetchManagerAssistantWorkspace,
  fetchManagerQuickHelpJob,
  fetchManagerSituationJob,
  fetchJob,
  fetchReportMarkdown,
  recordDealControlTaskEvent,
  recordManagerCommunicationCompleted,
  reviewDealControlCrmFact,
  saveDealControlScope,
  saveDealControlTaskOutcome,
  startAnalyze,
  startDealTaskGuidance,
  startManagerQuickHelp,
  startManagerSituationRefinement,
  syncDealControl,
  transcribeManagerVoice,
  updateDealControlDeal,
  updateDealControlBitrixTaskCompletion,
  updateDealControlChecklistItemCompletion,
  updateDealControlTask,
  type DealControlDashboard,
  type DealControlBitrixTask,
  type DealControlCommunicationsToday,
  type DealControlDeal,
  type DealControlTask,
  type DealControlTaskOutcome,
  type DealTaskGuidanceContent,
  type DealTaskGuidanceJob,
  type JobState,
  type ManagerQuickHelpContent,
  type ManagerQuickHelpEntry,
  type ManagerQuickHelpJob,
  type ManagerAssistantWorkspace,
  type ManagerSituationJob,
  type ManagerSituationState,
} from './api'
import { formatMoscowDateTime, moscowDateParts, parseMoscowDateTime } from './dateTime'

type DealControlView = 'dashboard' | 'rop' | 'manager'
type TimeView = 'all' | 'attention' | 'today' | 'tomorrow' | 'future' | 'overdue'

const BITRIX_DEAL_BASE_URL = 'https://obtorg.bitrix24.ru/crm/deal/details'
const BITRIX_ORIGIN = 'https://obtorg.bitrix24.ru'

const OUTCOME_LABELS: Record<DealControlTaskOutcome['result_status'], string> = {
  pending: 'Результат пока не получен',
  achieved: 'Цель задачи достигнута',
  partial: 'Получен частичный результат',
  postponed: 'Клиент перенёс решение',
  refused: 'Получен отказ',
  not_applicable: 'Задача потеряла актуальность',
  needs_rop_review: 'Нужна помощь РОПа',
}

const CONTACT_LABELS: Record<DealControlTaskOutcome['contact_status'], string> = {
  not_attempted: 'Действия ещё не было',
  attempt_no_contact: 'Была попытка, клиент не ответил',
  confirmed_contact: 'Контакт с клиентом состоялся',
  unknown: 'Контакт не подтверждён',
}

const EXECUTION_LABELS: Record<DealControlTask['crm_execution_status'], string> = {
  not_reflected: 'Не отражено в Bitrix',
  crm_open: 'Есть открытая задача',
  crm_closed: 'Задача закрыта в Bitrix',
  match_review: 'Проверить совпадение',
}

const VIEW_COPY: Record<DealControlView, { title: string; subtitle: string }> = {
  dashboard: {
    title: 'Дашборд сделок',
    subtitle: 'Быстрый обзор всех сделок, их состояния и ключевых метрик',
  },
  rop: {
    title: 'Контроль сделок РОПа',
    subtitle: 'Что просрочено, что на сегодня и как помочь менеджеру довести сделку',
  },
  manager: {
    title: 'Мои задачи по сделкам',
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

function bitrixTaskStatus(task: DealControlBitrixTask) {
  if (task.time_bucket === 'overdue') return 'Bitrix: задача просрочена'
  if (task.time_bucket === 'today') return 'Bitrix: задача на сегодня'
  if (task.time_bucket === 'tomorrow') return 'Bitrix: задача на завтра'
  if (task.time_bucket === 'unscheduled') return 'Bitrix: задача без срока'
  return 'Bitrix: задача открыта'
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

function textOr(value: string | undefined, fallback: string) {
  return value?.trim() || fallback
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

function outcomeValidationMessage(
  contact: DealControlTaskOutcome['contact_status'],
  result: DealControlTaskOutcome['result_status'],
  note: string,
  nextStep: string,
  nextAt: string,
) {
  const hasNote = Boolean(note.trim())
  const hasNextStep = Boolean(nextStep.trim() && nextAt)
  if (contact === 'not_attempted') return 'Сначала выполни действие или зафиксируй попытку контакта.'
  if (contact === 'unknown' && !hasNote) return 'Опиши, почему контакт с клиентом не подтверждён.'
  if (contact === 'attempt_no_contact' && (!hasNote || !hasNextStep)) {
    return 'Для попытки без ответа укажи, что произошло, следующий шаг и его срок.'
  }
  if (contact === 'confirmed_contact' && !hasNote) return 'Кратко зафиксируй подтверждённый ответ клиента.'
  if (result === 'pending' && !hasNextStep) return 'Для незавершённой задачи укажи следующий шаг и его срок.'
  if (['achieved', 'partial', 'postponed'].includes(result) && !hasNextStep) {
    return 'Для этого результата укажи следующий шаг и его срок.'
  }
  if (['refused', 'not_applicable'].includes(result) && !hasNote) {
    return 'Укажи причину отказа или потери актуальности.'
  }
  if (result === 'needs_rop_review' && !hasNote) return 'Опиши, какая помощь РОПа требуется.'
  return ''
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
  return situation.is_current && ['confirmed', 'refined'].includes(situation.state)
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

function appendVoiceText(current: string, transcript: string) {
  const next = transcript.trim()
  if (!next) return current
  if (!current.trim()) return next
  return `${current.trim()}\n${next}`
}

export function DealControl({ onExit }: { onExit?: () => void }) {
  const [data, setData] = useState<DealControlDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [, setNotice] = useState('')
  const [view, setView] = useState<DealControlView>('dashboard')
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
  const [pipelineId, setPipelineId] = useState('15')
  const [taskText, setTaskText] = useState('')
  const [touchType, setTouchType] = useState('Звонок')
  const [expectedResult, setExpectedResult] = useState('')
  const [dueAt, setDueAt] = useState('')
  const [rescheduleTask, setRescheduleTask] = useState<DealControlTask | null>(null)
  const [rescheduleAt, setRescheduleAt] = useState('')
  const [rescheduleReason, setRescheduleReason] = useState('')
  const [analysisJob, setAnalysisJob] = useState<JobState | null>(null)
  const [analyzingDealId, setAnalyzingDealId] = useState('')
  const [analysisConfirmDeal, setAnalysisConfirmDeal] = useState<DealControlDeal | null>(null)
  const [guidanceJob, setGuidanceJob] = useState<DealTaskGuidanceJob | null>(null)
  const [guidanceTaskId, setGuidanceTaskId] = useState<number | null>(null)
  const [outcomeTask, setOutcomeTask] = useState<DealControlTask | null>(null)
  const [outcomeContact, setOutcomeContact] = useState<DealControlTaskOutcome['contact_status']>('not_attempted')
  const [outcomeResult, setOutcomeResult] = useState<DealControlTaskOutcome['result_status']>('pending')
  const [outcomeNote, setOutcomeNote] = useState('')
  const [outcomeNextStep, setOutcomeNextStep] = useState('')
  const [outcomeNextAt, setOutcomeNextAt] = useState('')

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const response = await fetchDealControl()
      setData(response)
      setInitialIds(response.scope.initial_deal_ids.join('\n'))
      setManagerIds(response.scope.manager_ids.join('\n'))
      setPipelineId(response.scope.pipeline_id || '15')
      setSelectedId((current) => current || response.deals[0]?.deal_id || '')
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

  const guidanceJobId = guidanceJob?.job_id
  const guidanceJobStatus = guidanceJob?.status
  useEffect(() => {
    if (!guidanceJobId || !['queued', 'running'].includes(guidanceJobStatus || '')) return
    let cancelled = false
    let terminalHandled = false
    const poll = async () => {
      try {
        const next = await fetchDealTaskGuidanceJob(guidanceJobId)
        if (cancelled) return
        setGuidanceJob(next)
        if (!terminalHandled && next.status === 'done') {
          terminalHandled = true
          setNotice('AI-подсказка связана с активной задачей и готова для менеджера.')
          await reload()
        } else if (!terminalHandled && next.status === 'error') {
          terminalHandled = true
          setError(next.error || 'Не удалось подготовить менеджера')
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1200)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [guidanceJobId, guidanceJobStatus, reload])

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
        const rank = timeRank(a) - timeRank(b)
        const firstAt = primaryBitrixTaskOf(a)?.deadline
        const secondAt = primaryBitrixTaskOf(b)?.deadline
        return rank || String(firstAt || '').localeCompare(String(secondAt || ''))
      })
  }, [filteredDeals, timeView, view])

  const selected = visibleDeals.find((deal) => deal.deal_id === selectedId) || null
  const selectedTask = selected ? currentTaskOf(selected) : null

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
    if (!visibleIds.has(selectedId)) setSelectedId(visibleDeals[0]?.deal_id || '')
  }, [selectedId, visibleDeals])

  useEffect(() => {
    const guidanceId = selectedTask?.guidance && !selectedTask.guidance.is_stale
      ? selectedTask.guidance.id
      : null
    if (view !== 'manager' || !selectedTask || !guidanceId) return
    void recordDealControlTaskEvent(
      selectedTask.id,
      'guidance_opened',
      `guidance_opened:${guidanceId}`,
    ).catch(() => undefined)
  }, [selectedTask, view])

  async function sync() {
    setSyncing(true)
    setError('')
    setNotice('')
    try {
      const response = await syncDealControl()
      setData(response)
      setSelectedId((current) => current || response.deals[0]?.deal_id || '')
      setNotice(response.sync_message || 'Данные из Bitrix обновлены')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSyncing(false)
    }
  }

  async function saveScope() {
    setError('')
    try {
      await saveDealControlScope({
        initial_deal_ids: splitIds(initialIds),
        manager_ids: splitIds(managerIds),
        pipeline_id: pipelineId.trim(),
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

  async function addTask() {
    if (!selected) return
    setError('')
    try {
      await createDealControlTask(selected.deal_id, {
        task_text: taskText.trim(),
        touch_type: touchType,
        expected_result: expectedResult.trim() || null,
        due_at: dueAt,
      })
      setTaskText('')
      setExpectedResult('')
      setDueAt('')
      setNotice('Поручение сохранено локально. Менеджеру нужно отразить его в Bitrix вручную.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function adoptBitrixTask(deal: DealControlDeal, bitrixTask: DealControlBitrixTask) {
    const expected = 'Зафиксировать подтверждённый результат и следующий шаг'
    if (!bitrixTask.deadline) {
      setSelectedId(deal.deal_id)
      setTaskText(bitrixTask.subject)
      setTouchType('Задача Bitrix')
      setExpectedResult(expected)
      setDueAt('')
      setNotice('Поручение подготовлено из задачи Bitrix. Укажите срок и сохраните его.')
      return
    }
    setError('')
    try {
      await createDealControlTask(deal.deal_id, {
        task_text: bitrixTask.subject,
        touch_type: 'Задача Bitrix',
        expected_result: expected,
        due_at: bitrixTask.deadline,
      })
      setNotice('Задача Bitrix взята под контроль РОПа без создания дубликата в CRM.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function patchTask(task: DealControlTask, patch: Parameters<typeof updateDealControlTask>[1]) {
    setError('')
    try {
      await updateDealControlTask(task.id, patch)
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function toggleBitrixCompletion(deal: DealControlDeal, task: DealControlBitrixTask) {
    setError('')
    try {
      const completed = task.completion_state !== 'local'
      await updateDealControlBitrixTaskCompletion(
        deal.deal_id,
        task.activity_id,
        completed,
        view === 'manager' ? 'manager' : 'rop',
      )
      setNotice(completed ? 'Задача отмечена выполненной в приложении.' : 'Задача возвращена в работу.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function toggleChecklistItem(deal: DealControlDeal, itemId: string, completed: boolean) {
    setError('')
    try {
      await updateDealControlChecklistItemCompletion(deal.deal_id, itemId, completed)
      setNotice(completed ? 'Пункт чек-листа выполнен.' : 'Пункт чек-листа возвращён в работу.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function confirmMatch(task: DealControlTask) {
    try {
      await confirmDealControlTaskCrmMatch(task.id)
      setNotice('Совпадение с задачей Bitrix подтверждено. Бизнес-результат по-прежнему отмечается отдельно.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function reviewCrmFact(task: DealControlTask, factId: number, reviewStatus: 'confirmed' | 'rejected') {
    setError('')
    try {
      await reviewDealControlCrmFact(task.id, factId, { review_status: reviewStatus })
      setNotice(reviewStatus === 'confirmed'
        ? 'CRM-факт подтверждён как относящийся к задаче. Контакт с клиентом по-прежнему учитывается отдельно.'
        : 'CRM-факт исключён из результата этой задачи.')
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
      const guidanceId = selectedTask?.guidance && !selectedTask.guidance.is_stale
        ? selectedTask.guidance.id
        : null
      if (view === 'manager' && selectedTask && guidanceId) {
        void recordDealControlTaskEvent(
          selectedTask.id,
          'guidance_copied',
          `guidance_copied:${guidanceId}`,
        ).catch(() => undefined)
      }
      setNotice(`${label} скопирован. В Bitrix его нужно перенести вручную.`)
    } catch {
      setError('Не удалось скопировать текст. Разрешите браузеру доступ к буферу обмена.')
    }
  }

  async function runAnalyzeDeal(deal: DealControlDeal, confirmPaid = false) {
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

  async function prepareManager(task: DealControlTask) {
    if (guidanceJob && ['queued', 'running'].includes(guidanceJob.status)) return
    setError('')
    setNotice('')
    setGuidanceTaskId(task.id)
    try {
      const started = await startDealTaskGuidance(task.id)
      setGuidanceJob(started)
      setNotice('Готовим менеджера именно к текущей задаче РОПа.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  function beginReschedule(task: DealControlTask) {
    setRescheduleTask(task)
    setRescheduleAt(task.due_at.slice(0, 16))
    setRescheduleReason('')
  }

  async function applyReschedule() {
    if (!rescheduleTask || !rescheduleAt) return
    await patchTask(rescheduleTask, {
      due_at: rescheduleAt,
      reschedule_reason: rescheduleReason.trim() || null,
      source_role: view === 'manager' ? 'manager' : 'rop',
    })
    setRescheduleTask(null)
    setRescheduleAt('')
    setRescheduleReason('')
    setNotice('Срок перенесён локально. Обновите соответствующую задачу в Bitrix вручную.')
  }

  function beginOutcome(task: DealControlTask) {
    const latest = task.latest_outcome
    setOutcomeTask(task)
    setOutcomeContact(latest?.contact_status || 'not_attempted')
    setOutcomeResult(latest?.result_status || 'pending')
    setOutcomeNote(latest?.result_note || '')
    setOutcomeNextStep(latest?.next_step_text || '')
    setOutcomeNextAt((latest?.next_step_at || '').slice(0, 16))
  }

  async function applyOutcome() {
    if (!outcomeTask) return
    const validationError = outcomeValidationMessage(
      outcomeContact,
      outcomeResult,
      outcomeNote,
      outcomeNextStep,
      outcomeNextAt,
    )
    if (validationError) {
      setError(validationError)
      return
    }
    setError('')
    try {
      await saveDealControlTaskOutcome(outcomeTask.id, {
        contact_status: outcomeContact,
        result_status: outcomeResult,
        result_note: outcomeNote.trim() || null,
        next_step_text: outcomeNextStep.trim() || null,
        next_step_at: outcomeNextAt || null,
        evidence_kind: outcomeContact === 'confirmed_contact'
          ? view === 'manager' ? 'manager_confirmation' : 'rop_confirmation'
          : null,
        source_role: view === 'manager' ? 'manager' : 'rop',
      })
      setOutcomeTask(null)
      setNotice('Результат сохранён отдельно от отметки Bitrix. РОП увидит контакт, итог и следующий шаг.')
      await reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  if (loading && !data) {
    return <main className="dc-shell dc-loading"><span className="dc-spinner" />Загружается контроль сделок…</main>
  }

  if (!data?.scope.configured) {
    return <main className="dc-setup">
      <section>
        <span className="dc-eyebrow">Первичная настройка</span>
        <h1>Контроль сделок</h1>
        <p>Сохраняем локальную выборку. Bitrix используется только для чтения.</p>
        <label>Стартовые ID сделок<textarea value={initialIds} onChange={(event) => setInitialIds(event.target.value)} /></label>
        <label>ID ответственных для новых сделок<textarea value={managerIds} onChange={(event) => setManagerIds(event.target.value)} /></label>
        <label>Воронка Bitrix<input value={pipelineId} onChange={(event) => setPipelineId(event.target.value)} /></label>
        <div><button className="dc-button primary" onClick={() => void saveScope()}>Сохранить выборку</button>{onExit ? <button className="dc-button" onClick={onExit}>Назад</button> : null}</div>
        {error ? <p className="dc-alert error">{error}</p> : null}
      </section>
    </main>
  }

  const copyForView = VIEW_COPY[view]
  const outcomeError = outcomeValidationMessage(
    outcomeContact,
    outcomeResult,
    outcomeNote,
    outcomeNextStep,
    outcomeNextAt,
  )

  return <main className={`dc-shell ${menuOpen ? 'menu-open' : ''}`}>
    <aside className="dc-sidebar">
      <button className="dc-menu-button" onClick={() => setMenuOpen((value) => !value)} title="Развернуть меню">
        <span>☰</span><b>Меню</b>
      </button>
      <nav>
        <button className={view === 'dashboard' ? 'active' : ''} onClick={() => {
          setView('dashboard')
          setTimeView('all')
        }} title="Дашборд">
          <span>▦</span><b>Дашборд</b><small>Общий контроль сделок</small>
        </button>
        <button className={view === 'rop' ? 'active' : ''} onClick={() => {
          setView('rop')
          setTimeView('today')
        }} title="Контроль РОПа">
          <span>◎</span><b>Контроль РОПа</b><small>План и просрочки команды</small>
        </button>
        <button className={view === 'manager' ? 'active' : ''} onClick={() => {
          setView('manager')
          setManagerFilter(selected?.manager_id || managers[0]?.[0] || '')
          setTimeView('today')
        }} title="Задачи менеджера">
          <span>✓</span><b>Мои задачи</b><small>Подготовка к касаниям</small>
        </button>
      </nav>
      {onExit ? <button className="dc-exit" onClick={onExit}><span>←</span><b>К основному интерфейсу</b></button> : null}
    </aside>

    <section className="dc-content">
      <header className="dc-header">
        <div><h1>{copyForView.title}</h1></div>
        <div className="dc-refresh">
          <span>Обновлено {dateTime(data.generated_at)}</span>
          <button className="dc-button" disabled={syncing} onClick={() => void sync()}>
            {syncing ? <><span className="dc-spinner" />Обновляем Bitrix…</> : <><span>⟳</span>Обновить Bitrix</>}
          </button>
        </div>
      </header>

      {error ? <div className="dc-alert error">{error}</div> : null}
      {data.sync_errors.length ? <details className="dc-sync-errors"><summary>Bitrix обновлён с ограничениями: {data.sync_errors.length}</summary><ul>{data.sync_errors.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}

      <Kpis view={view} summary={filteredSummary} />
      <Filters
        view={view}
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
            <div><h2>{view === 'dashboard' ? 'Обзор портфеля сделок' : taskPlanTitle(timeView)}</h2></div>
            <span>{view === 'dashboard' ? 'Сначала критичные ›' : 'Фокус дня ›'}</span>
          </div>
          {view === 'dashboard'
            ? <DealTable deals={visibleDeals} selectedId={selected?.deal_id || ''} onSelect={setSelectedId} onSaveFields={saveFields} onReschedule={beginReschedule} />
            : <TaskTable view={view} deals={visibleDeals} selectedId={selected?.deal_id || ''} onSelect={setSelectedId} />
          }
        </section>

        <div className="dc-resizer" onPointerDown={(event) => { event.preventDefault(); setDragging(true) }} title="Потяните, чтобы изменить ширину">⋮</div>

        <DealDetail
          view={view}
          deal={selected}
          onReload={reload}
          onConfirmMatch={confirmMatch}
          onReviewFact={reviewCrmFact}
          onReschedule={beginReschedule}
          onPrepareManager={prepareManager}
          onOutcome={beginOutcome}
          onCopy={copy}
          taskText={taskText}
          setTaskText={setTaskText}
          touchType={touchType}
          setTouchType={setTouchType}
          expectedResult={expectedResult}
          setExpectedResult={setExpectedResult}
          dueAt={dueAt}
          setDueAt={setDueAt}
          onAddTask={addTask}
          onAdoptBitrixTask={adoptBitrixTask}
          onToggleBitrixCompletion={toggleBitrixCompletion}
          onToggleChecklistItem={toggleChecklistItem}
          analysisJob={analysisJob}
          analyzingDealId={analyzingDealId}
          onAnalyze={analyzeDeal}
          guidanceJob={guidanceJob}
          guidanceTaskId={guidanceTaskId}
        />
      </div>
    </section>

    {rescheduleTask ? <div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) setRescheduleTask(null) }}>
      <section className="dc-modal">
        <h2>Перенести срок задачи</h2>
        <p>{compactTaskText(rescheduleTask.task_text)}</p>
        <label>Новая дата и время<input type="datetime-local" value={rescheduleAt} onChange={(event) => setRescheduleAt(event.target.value)} /></label>
        {view === 'rop' ? <label>Причина переноса<textarea value={rescheduleReason} onChange={(event) => setRescheduleReason(event.target.value)} placeholder="Почему РОП меняет согласованный срок" /></label> : null}
        <div><button className="dc-button" onClick={() => setRescheduleTask(null)}>Отмена</button><button className="dc-button primary" disabled={!rescheduleAt || (view === 'rop' && !rescheduleReason.trim())} onClick={() => void applyReschedule()}>Перенести</button></div>
      </section>
    </div> : null}

    {outcomeTask ? <div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) setOutcomeTask(null) }}>
      <section className="dc-modal dc-outcome-modal">
        <h2>{view === 'manager' ? 'Зафиксировать результат' : 'Скорректировать результат'}</h2>
        <p>{compactTaskText(outcomeTask.task_text)}</p>
        <label>Контакт с клиентом<select value={outcomeContact} onChange={(event) => setOutcomeContact(event.target.value as DealControlTaskOutcome['contact_status'])}>{Object.entries(CONTACT_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>Результат задачи<select value={outcomeResult} onChange={(event) => setOutcomeResult(event.target.value as DealControlTaskOutcome['result_status'])}>{Object.entries(OUTCOME_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>Что произошло<textarea value={outcomeNote} onChange={(event) => setOutcomeNote(event.target.value)} placeholder="Коротко зафиксируй ответ клиента или причину отсутствия результата" /></label>
        <label>Следующий шаг<input value={outcomeNextStep} onChange={(event) => setOutcomeNextStep(event.target.value)} placeholder="Например: отправить уточнённый расчёт" /></label>
        <label>Срок следующего шага<input type="datetime-local" value={outcomeNextAt} onChange={(event) => setOutcomeNextAt(event.target.value)} /></label>
        <small className={outcomeError ? 'dc-form-error' : ''}>{outcomeError || 'Контакт, результат и следующий шаг сохраняются отдельно от отметки задачи в Bitrix.'}</small>
        <div><button className="dc-button" onClick={() => setOutcomeTask(null)}>Отмена</button><button className="dc-button primary" disabled={Boolean(outcomeError)} onClick={() => void applyOutcome()}>{view === 'manager' ? 'Сохранить результат' : 'Сохранить корректировку'}</button></div>
      </section>
    </div> : null}

    {analysisConfirmDeal ? <div className="dc-modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) setAnalysisConfirmDeal(null) }}>
      <section className="dc-modal dc-analysis-confirm">
        <span>✦</span>
        <h2>Проверить новые данные?</h2>
        <p>Система обновит информацию из Bitrix и проверит новые звонки. Если появились существенные изменения, могут потребоваться платная транскрибация и новый AI-анализ.</p>
        <small>Без значимых изменений текущий анализ останется актуальным, повторный LLM-анализ не запустится.</small>
        <div><button className="dc-button" onClick={() => setAnalysisConfirmDeal(null)}>Отмена</button><button className="dc-button primary" onClick={() => { const deal = analysisConfirmDeal; setAnalysisConfirmDeal(null); void runAnalyzeDeal(deal, true) }}>Проверить и обновить</button></div>
      </section>
    </div> : null}
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
    <select value={props.managerFilter} onChange={(event) => props.onManager(event.target.value)}>
      <option value="">{props.view === 'manager' ? 'Выберите менеджера' : 'Все менеджеры'}</option>
      {props.managers.map(([id, name]) => <option value={id} key={id}>{name}</option>)}
    </select>
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
  onReschedule: (task: DealControlTask) => void
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

function ControlSyncState({ task, bitrixTask, compact = false }: {
  task: DealControlTask | null
  bitrixTask: DealControlBitrixTask | null
  compact?: boolean
}) {
  const bitrixLabel = task?.crm_execution_status === 'match_review'
    ? 'Bitrix: задача найдена — подтвердить связь'
    : task?.crm_execution_status === 'crm_closed'
      ? 'Bitrix: задача закрыта'
      : task?.crm_execution_status === 'crm_open'
          ? 'Bitrix: задача открыта'
          : task && bitrixTask
            ? 'Bitrix: открытая задача есть — не связана'
            : bitrixTask
              ? bitrixTaskStatus(bitrixTask)
          : 'Bitrix: задача не найдена'
  const bitrixTone = task?.crm_execution_status === 'match_review'
    ? 'warn'
    : task?.crm_execution_status === 'not_reflected' && bitrixTask
      ? 'warn'
      : bitrixTask || task?.crm_execution_status === 'crm_open' || task?.crm_execution_status === 'crm_closed'
      ? 'ok'
      : 'missing'
  return <div className={`dc-sync-state ${compact ? 'compact' : ''}`}>
    <span className={task ? 'ok' : 'muted'}>{task ? '✓ Поручение РОПа: сохранено' : 'Поручение РОПа: не добавлено'}</span>
    <span className={bitrixTone}>{bitrixLabel}</span>
  </div>
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
  deal: DealControlDeal | null
  onReload: () => Promise<void>
  onConfirmMatch: (task: DealControlTask) => Promise<void>
  onReviewFact: (task: DealControlTask, factId: number, reviewStatus: 'confirmed' | 'rejected') => Promise<void>
  onReschedule: (task: DealControlTask) => void
  onPrepareManager: (task: DealControlTask) => Promise<void>
  onOutcome: (task: DealControlTask) => void
  onCopy: (text: string, label: string) => Promise<void>
  taskText: string
  setTaskText: (value: string) => void
  touchType: string
  setTouchType: (value: string) => void
  expectedResult: string
  setExpectedResult: (value: string) => void
  dueAt: string
  setDueAt: (value: string) => void
  onAddTask: () => Promise<void>
  onAdoptBitrixTask: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
  onToggleBitrixCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
  analysisJob: JobState | null
  analyzingDealId: string
  onAnalyze: (deal: DealControlDeal) => Promise<void>
  guidanceJob: DealTaskGuidanceJob | null
  guidanceTaskId: number | null
}) {
  const [showAnalysisMarkdown, setShowAnalysisMarkdown] = useState(false)
  const [analysisMarkdown, setAnalysisMarkdown] = useState<string | null>(null)
  const [analysisMarkdownError, setAnalysisMarkdownError] = useState('')
  const [analysisMarkdownLoading, setAnalysisMarkdownLoading] = useState(false)
  const [situationModalOpen, setSituationModalOpen] = useState(false)
  const [situationContext, setSituationContext] = useState('')
  const [situationError, setSituationError] = useState('')
  const [situationJob, setSituationJob] = useState<ManagerSituationJob | null>(null)
  const [quickHelpDraft, setQuickHelpDraft] = useState('')
  const [quickHelpError, setQuickHelpError] = useState('')
  const [quickHelpJob, setQuickHelpJob] = useState<ManagerQuickHelpJob | null>(null)
  const [assistantWorkspace, setAssistantWorkspace] = useState<ManagerAssistantWorkspace | null>(null)
  const [assistantLoading, setAssistantLoading] = useState(false)
  const [assistantOpen, setAssistantOpen] = useState(false)
  const activeReportId = props.deal?.coaching.report_id
  const activeDealId = props.deal?.deal_id || ''
  const detailView = props.view
  const reloadDetail = props.onReload

  useEffect(() => {
    setShowAnalysisMarkdown(false)
    setAnalysisMarkdown(null)
    setAnalysisMarkdownError('')
    setAnalysisMarkdownLoading(false)
  }, [props.deal?.deal_id, activeReportId])

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
  }, [activeDealId, activeReportId])

  useEffect(() => {
    writeDealDraft(MANAGER_SITUATION_DRAFT_PREFIX, activeDealId, situationContext)
  }, [activeDealId, situationContext])

  useEffect(() => {
    writeDealDraft(MANAGER_QUICK_HELP_DRAFT_PREFIX, activeDealId, quickHelpDraft)
  }, [activeDealId, quickHelpDraft])

  const loadAssistantWorkspace = useCallback(async (open = false) => {
    if (!activeDealId) return null
    setAssistantLoading(true)
    try {
      const workspace = await fetchManagerAssistantWorkspace(activeDealId)
      setAssistantWorkspace(workspace)
      if (open) setAssistantOpen(true)
      return workspace
    } catch (reason) {
      setQuickHelpError(reason instanceof Error ? reason.message : 'Не удалось загрузить помощника')
      return null
    } finally {
      setAssistantLoading(false)
    }
  }, [activeDealId])

  useEffect(() => {
    if (detailView !== 'manager' || !props.deal || !managerSituationIsConfirmed(managerSituationOf(props.deal))) return
    let cancelled = false
    void fetchManagerAssistantWorkspace(activeDealId)
      .then((workspace) => { if (!cancelled) setAssistantWorkspace(workspace) })
      .catch(() => { /* the main situation card already explains why help is unavailable */ })
    return () => { cancelled = true }
  }, [activeDealId, activeReportId, detailView, props.deal])

  const situationJobId = situationJob?.job_id
  const situationJobStatus = situationJob?.status
  useEffect(() => {
    if (detailView !== 'manager' || !situationJobId || !['queued', 'running'].includes(situationJobStatus || '')) return
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
          setSituationModalOpen(false)
          setSituationContext('')
          setSituationError('')
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
  }, [activeDealId, detailView, reloadDetail, situationJobId, situationJobStatus])

  const quickHelpJobId = quickHelpJob?.job_id
  const quickHelpJobStatus = quickHelpJob?.status
  useEffect(() => {
    if (detailView !== 'manager' || !quickHelpJobId || !['queued', 'running'].includes(quickHelpJobStatus || '')) return
    let cancelled = false
    let terminalHandled = false
    const poll = async () => {
      try {
        const next = await fetchManagerQuickHelpJob(quickHelpJobId)
        if (cancelled || next.deal_id !== activeDealId) return
        setQuickHelpJob(next)
        if (terminalHandled) return
        if (next.status === 'done') {
          terminalHandled = true
          if (!cancelled) await loadAssistantWorkspace(true)
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
  }, [activeDealId, detailView, loadAssistantWorkspace, quickHelpJobId, quickHelpJobStatus])

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
    try {
      const started = await startManagerSituationRefinement(props.deal.deal_id, context, true)
      setSituationJob(started)
      if (started.status === 'error') setSituationError(started.error || 'Не удалось пересобрать текущую ситуацию')
      if (started.status === 'done') {
        setSituationModalOpen(false)
        setSituationContext('')
        await props.onReload()
      }
    } catch (reason) {
      setSituationError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  async function requestQuickHelp(question: string) {
    if (!props.deal) return
    const normalized = question.trim()
    if (normalized.length < 1 || normalized.length > 4000) {
      setQuickHelpError('Опиши вопрос от 1 до 4000 символов.')
      return
    }
    if (quickHelpJob && ['queued', 'running'].includes(quickHelpJob.status)) return
    setQuickHelpError('')
    try {
      const started = await startManagerQuickHelp(props.deal.deal_id, normalized, true)
      setQuickHelpDraft('')
      setQuickHelpJob(started)
      if (started.status === 'error') setQuickHelpError(started.error || 'Не удалось получить помощь тренера')
      if (started.status === 'done') {
        await loadAssistantWorkspace(true)
      }
    } catch (reason) {
      setQuickHelpError(reason instanceof Error ? reason.message : String(reason))
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

  async function toggleAnalysisMarkdown() {
    if (showAnalysisMarkdown) {
      setShowAnalysisMarkdown(false)
      return
    }
    if (!activeReportId) return
    if (analysisMarkdown) {
      setShowAnalysisMarkdown(true)
      return
    }
    setAnalysisMarkdownLoading(true)
    setAnalysisMarkdownError('')
    try {
      const response = await fetchReportMarkdown(activeReportId)
      setAnalysisMarkdown(response.markdown)
      setShowAnalysisMarkdown(true)
    } catch (error) {
      setAnalysisMarkdownError(error instanceof Error ? error.message : 'Markdown-отчёт недоступен')
    } finally {
      setAnalysisMarkdownLoading(false)
    }
  }

  async function transcribeVoice(audio: Blob) {
    if (!props.deal) throw new Error('Сделка не выбрана')
    const response = await transcribeManagerVoice(props.deal.deal_id, audio, true)
    if (!response.text?.trim()) throw new Error('Транскрибация не вернула текст. Попробуй ещё раз или введи текст вручную.')
    return response.text.trim()
  }

  if (!props.deal) return <aside className="dc-detail"><p className="dc-empty">Выберите сделку в таблице.</p></aside>
  const deal = props.deal
  const task = currentTaskOf(deal)
  const coaching = deal.coaching
  const managerView = props.view === 'manager'
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
        : <><span>✦</span>{hasAnalysis ? 'Обновить анализ' : 'Провести анализ'}</>}
    </button>
  )
  const analysisReady = (
    <div className="dc-analysis-ready">
      <div><span>✓</span><strong>AI-анализ проведён</strong></div>
      {coaching.analysis_created_at ? <small>{dateTime(coaching.analysis_created_at)}</small> : null}
      <button className="dc-button" disabled={analysisBusy} onClick={() => void props.onAnalyze(deal)}>
        {analysisRunning ? <><span className="dc-spinner" />Обновляем…</> : 'Обновить'}
      </button>
    </div>
  )

  return <aside className="dc-detail">
    <header>
      <div><div className="dc-deal-title-row"><h2>Сделка #{deal.deal_id}</h2><a className="dc-button primary dc-bitrix-detail-link" href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer">B24 ↗</a></div><p>{deal.title}</p></div>
      {hasAnalysis ? analysisReady : null}
    </header>
    {!managerView && analysisRunning && props.analysisJob
      ? <DealAnalysisProgress job={props.analysisJob} dealId={deal.deal_id} />
      : null}
    <section className="dc-detail-stats">
      <div><small>Этап</small><strong>{deal.stage_name || '—'}</strong></div>
      <div><small>Вероятность</small><strong>{deal.probability == null ? '—' : `${deal.probability}%`}</strong></div>
      <div><small>Менеджер</small><strong>{deal.manager_name || '—'}</strong></div>
      <div><small>Сумма</small><strong>{money(deal.amount, deal.currency_id || 'RUB')}</strong></div>
    </section>
    {managerView && analysisRunning && props.analysisJob ? <DealAnalysisProgress job={props.analysisJob} dealId={deal.deal_id} /> : null}

    {managerView ? <ManagerDealScreen
      deal={deal}
      situation={managerSituation}
      hasAnalysis={hasAnalysis}
      analysisEmptyAction={analysisButton}
      situationModalOpen={situationModalOpen}
      situationContext={situationContext}
      situationError={situationError}
      situationJob={situationJob}
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
      onQuickHelpDraft={setQuickHelpDraft}
      onQuickHelp={requestQuickHelp}
      onOpenAssistant={() => void loadAssistantWorkspace(true)}
      onCloseAssistant={() => setAssistantOpen(false)}
      onCompleteCommunication={(quickHelpId) => void completeAssistantCommunication(quickHelpId)}
      onCopy={props.onCopy}
      onTranscribe={transcribeVoice}
      onToggleBitrixCompletion={props.onToggleBitrixCompletion}
      onToggleChecklistItem={props.onToggleChecklistItem}
    /> : props.view === 'rop' ? <RopDealScreen
      deal={deal}
      hasAnalysis={hasAnalysis}
      analysisEmptyAction={analysisButton}
    /> : <>
    {hasAnalysis ? <DealSituationCard deal={deal} /> : <section className="dc-analysis-empty">
      <span>✦</span>
      <div><h3>Анализ не проведён</h3><p>Проведите анализ, чтобы увидеть текущую ситуацию, риски и рекомендации по сделке.</p></div>
      {analysisButton}
    </section>}

    {hasAnalysis ? <CurrentTask
      view={props.view}
      deal={deal}
      task={task}
      onConfirmMatch={props.onConfirmMatch}
      onReviewFact={props.onReviewFact}
      onReschedule={props.onReschedule}
      onPrepareManager={props.onPrepareManager}
      onOutcome={props.onOutcome}
      guidanceJob={props.guidanceJob}
      guidanceTaskId={props.guidanceTaskId}
      hasAnalysis={hasAnalysis}
      onAdoptBitrixTask={props.onAdoptBitrixTask}
      onToggleBitrixCompletion={props.onToggleBitrixCompletion}
    /> : null}

    {hasAnalysis
      ? <RopGuidance deal={deal} task={task} onCopy={props.onCopy} />
      : null}

    {hasAnalysis && props.view !== 'manager' && deal.current_task ? <TaskEditor
      taskText={props.taskText}
      setTaskText={props.setTaskText}
      touchType={props.touchType}
      setTouchType={props.setTouchType}
      expectedResult={props.expectedResult}
      setExpectedResult={props.setExpectedResult}
      dueAt={props.dueAt}
      setDueAt={props.setDueAt}
      hint={coaching.rop_task_hint}
      expectedHint={coaching.expected_crm_update}
      onAddTask={props.onAddTask}
    /> : null}

    {hasAnalysis ? <section className="dc-analysis-material">
      <button
        className="dc-analysis-material-link"
        disabled={analysisMarkdownLoading}
        onClick={() => void toggleAnalysisMarkdown()}
      >
        {analysisMarkdownLoading
          ? 'Открываем материал…'
          : showAnalysisMarkdown
            ? 'Скрыть Markdown анализа'
            : 'Открыть Markdown анализа'}
      </button>
      {analysisMarkdownError ? <small>{analysisMarkdownError}</small> : null}
      {showAnalysisMarkdown && analysisMarkdown
        ? <pre>{analysisMarkdown}</pre>
        : null}
    </section> : null}
    </>}
  </aside>
}

type ManagerDealScreenProps = {
  deal: DealControlDeal
  situation: ManagerSituationState
  hasAnalysis: boolean
  analysisEmptyAction: ReactNode
  situationModalOpen: boolean
  situationContext: string
  situationError: string
  situationJob: ManagerSituationJob | null
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
  onQuickHelpDraft: (value: string) => void
  onQuickHelp: (question: string) => Promise<void>
  onOpenAssistant: () => void
  onCloseAssistant: () => void
  onCompleteCommunication: (quickHelpId: number) => void
  onCopy: (text: string, label: string) => Promise<void>
  onTranscribe: (audio: Blob) => Promise<string>
  onToggleBitrixCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
  onToggleChecklistItem: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
}

function ManagerDealScreen(props: ManagerDealScreenProps) {
  const confirmed = managerSituationIsConfirmed(props.situation)
  return <>
    <ManagerSituationActions
      deal={props.deal}
      situation={props.situation}
      hasAnalysis={props.hasAnalysis}
      analysisEmptyAction={props.analysisEmptyAction}
      modalOpen={props.situationModalOpen}
      context={props.situationContext}
      error={props.situationError}
      job={props.situationJob}
      onOpenModal={props.onOpenSituation}
      onCloseModal={props.onCloseSituation}
      onContext={props.onSituationContext}
      onConfirm={props.onConfirmSituation}
      onRefine={props.onRefineSituation}
      onTranscribe={props.onTranscribe}
    />
    {confirmed ? <>
      <DealChecklistCard deal={props.deal} editable onToggle={props.onToggleChecklistItem} />
      <ManagerQuickHelp
        dealId={props.deal.deal_id}
        draft={props.quickHelpDraft}
        error={props.quickHelpError}
        job={props.quickHelpJob}
        started={Boolean(props.assistantWorkspace?.started)}
        loading={props.assistantLoading}
        onDraft={props.onQuickHelpDraft}
        onRequest={props.onQuickHelp}
        onOpen={props.onOpenAssistant}
        onTranscribe={props.onTranscribe}
      />
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
      /> : null}
    </> : null}
  </>
}

function RopDealScreen({ deal, hasAnalysis, analysisEmptyAction }: {
  deal: DealControlDeal
  hasAnalysis: boolean
  analysisEmptyAction: ReactNode
}) {
  return <>
    {!hasAnalysis ? <section className="dc-analysis-empty">
      <span>✦</span>
      <div><h3>Анализ не проведён</h3><p>Проведите анализ, чтобы сформировать чек-лист и текущий итог.</p></div>
      {analysisEmptyAction}
    </section> : null}
    {hasAnalysis ? <DealChecklistCard deal={deal} editable={false} /> : null}
    <DailyCommunicationWidget summary={deal.communications_today} />
    {hasAnalysis ? <RopCurrentSummary deal={deal} /> : null}
  </>
}

function DealChecklistCard({ deal, editable, onToggle }: {
  deal: DealControlDeal
  editable: boolean
  onToggle?: (deal: DealControlDeal, itemId: string, completed: boolean) => Promise<void>
}) {
  const checklist = deal.checklist || { items: [], completed: 0, total: 0, progress_percent: 0 }
  return <section className="dc-deal-checklist">
    <header>
      <span className="dc-deal-checklist-icon">✓</span>
      <div><h3>Чек-лист дожима</h3><p>Что ещё нужно закрыть, чтобы приблизить сделку к решению</p></div>
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
        <span>{item.text}</span>
        <em>{item.completed ? 'Выполнено' : 'Не выполнено'}</em>
      </li>)}</ul> : <p className="dc-deal-checklist-empty">Чек-лист появится после успешного анализа сделки.</p>}
    </div>
  </section>
}

function RopCurrentSummary({ deal }: { deal: DealControlDeal }) {
  const checklist = deal.checklist
  const remaining = checklist?.items.filter((item) => !item.completed).map((item) => item.text) || []
  const completed = checklist?.items.filter((item) => item.completed).map((item) => item.text) || []
  const analysisTime = deal.coaching.analysis_created_at
    ? formatMoscowDateTime(deal.coaching.analysis_created_at, { hour: '2-digit', minute: '2-digit' })
    : ''
  return <section className="dc-rop-current-summary">
    <header>
      <span>AI</span>
      <div><h3>{analysisTime ? `Итог на ${analysisTime}` : 'Текущий итог'}</h3><p>Срез сформирован из последнего сохранённого анализа и чек-листа</p></div>
      <strong>{deal.coaching.report_id ? 'Последний анализ' : 'Нет анализа'}</strong>
    </header>
    <div>
      <p>{deal.coaching.current_situation || 'Текущая ситуация пока не сформирована.'}</p>
      {completed.length ? <p><b>Менеджер закрыл:</b> {completed.join(' ')}</p> : null}
      {remaining.length ? <p><b>Осталось:</b> {remaining.join(' ')}</p> : null}
      <aside><b>Вывод для РОПа</b><span>{deal.coaching.rop_focus || deal.coaching.what_to_check_now || 'Управленческий вывод появится после анализа сделки.'}</span></aside>
    </div>
  </section>
}

function DealSituationCard({ deal }: { deal: DealControlDeal }) {
  const [focusExpanded, setFocusExpanded] = useState(false)
  const focusText = deal.coaching.what_to_check_now?.trim() || ''
  const canCollapseFocus = focusText.length > 115

  useEffect(() => {
    setFocusExpanded(false)
  }, [deal.deal_id, focusText])

  return <section className="dc-overview-situation">
    <header className="dc-manager-situation-head">
      <span className="dc-manager-situation-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M4 15.5h3l2.1-6 3.4 10 2.2-7H20" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" opacity=".42" />
        </svg>
      </span>
      <div className="dc-manager-situation-heading">
        <h3>Текущая ситуация</h3>
      </div>
      <span className="dc-manager-situation-status"><i />AI-анализ</span>
    </header>

    <div className="dc-manager-situation-body">
      <p className="dc-manager-situation-copy">{deal.coaching.current_situation || 'Текущая ситуация пока не сформирована.'}</p>
      {focusText ? <div className="dc-manager-situation-focus">
        <span className="dc-manager-situation-focus-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="8.5" stroke="currentColor" strokeWidth="1.8" />
            <path d="M12 7.5V12l3 2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </span>
        <div>
          <strong>Что проверить сейчас</strong>
          <p className={`dc-manager-situation-focus-text ${canCollapseFocus && !focusExpanded ? 'collapsed' : ''}`}>{focusText}</p>
          {canCollapseFocus ? <button
            className={`dc-manager-situation-focus-toggle ${focusExpanded ? 'open' : ''}`}
            type="button"
            onClick={() => setFocusExpanded((value) => !value)}
          >
            {focusExpanded ? 'Скрыть' : 'Показать полностью'}
            <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="M5 7.5l5 5 5-5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button> : null}
        </div>
      </div> : null}
    </div>

    <footer className="dc-overview-situation-focus">
      <small>Фокус РОПа</small>
      <strong>{textOr(deal.coaching.rop_focus, 'Не сформирован')}</strong>
    </footer>
  </section>
}

function ManagerSituationActions(props: {
  deal: DealControlDeal
  situation: ManagerSituationState
  hasAnalysis: boolean
  analysisEmptyAction: ReactNode
  modalOpen: boolean
  context: string
  error: string
  job: ManagerSituationJob | null
  onOpenModal: () => void
  onCloseModal: () => void
  onContext: (value: string) => void
  onConfirm: () => void
  onRefine: () => void
  onTranscribe: (audio: Blob) => Promise<string>
}) {
  const confirmed = managerSituationIsConfirmed(props.situation)
  const busy = Boolean(props.job && ['queued', 'running'].includes(props.job.status))
  const stateLabel = !props.situation.is_current || props.situation.state === 'pending'
    ? 'Требует проверки'
    : props.situation.state === 'refined' ? 'Уточнена менеджером' : 'Подтверждена'

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
        </div>
        <span className="dc-manager-situation-status"><i />{stateLabel}</span>
      </header>

      <div className="dc-manager-situation-body">
        {props.hasAnalysis
          ? <>
            <p className="dc-manager-situation-copy">{props.deal.coaching.current_situation || 'Текущая ситуация пока не сформирована.'}</p>
          </>
          : <div className="dc-analysis-empty dc-manager-analysis-empty">
            <span>✦</span><div><h3>Анализ не проведён</h3><p>Проведи полный анализ сделки, чтобы получить текущую ситуацию.</p></div>{props.analysisEmptyAction}
          </div>}
        {props.job ? <ManagerJobProgress job={props.job} label="Пересборка ситуации" /> : null}
        {props.error ? <p className="dc-manager-error" role="alert">{props.error}</p> : null}
      </div>

      {props.hasAnalysis ? <footer className="dc-manager-situation-actions">
        <button className="dc-button primary" disabled={busy || confirmed} onClick={props.onConfirm}>
          {confirmed ? '✓ Ситуация подтверждена' : 'Подтвердить ситуацию'}
        </button>
        <button className="dc-button" disabled={busy} onClick={props.onOpenModal}>
          {props.situation.state === 'refined' ? 'Изменить контекст' : 'Добавить контекст'}
        </button>
      </footer> : null}
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
  dealId: string
  draft: string
  error: string
  job: ManagerQuickHelpJob | null
  started: boolean
  loading: boolean
  onDraft: (value: string) => void
  onRequest: (question: string) => Promise<void>
  onOpen: () => void
  onTranscribe: (audio: Blob) => Promise<string>
}) {
  const busy = Boolean(props.job && ['queued', 'running'].includes(props.job.status))
  return <section className="dc-manager-quick-help">
    <div className="dc-section-head"><div><h3>{props.started ? 'Помощник менеджера' : 'Быстрая ИИ помощь менеджеру'}</h3>{props.started ? <p>Диалог по сделке уже начат</p> : null}</div><span>AI</span></div>
    {props.started ? <button className="dc-button primary dc-manager-assistant-open" disabled={props.loading} onClick={props.onOpen}>{props.loading ? <><span className="dc-spinner" />Открываем…</> : 'Открыть помощника'}</button> : <>
      <div className="dc-manager-voice-field dc-manager-quick-help-field">
        <textarea value={props.draft} maxLength={4000} onChange={(event) => props.onDraft(event.target.value)} placeholder="Опишите ситуацию или задайте вопрос..." aria-label="Вопрос помощнику менеджера" />
        <ManagerVoiceInput dealId={props.dealId} disabled={busy} onTranscribe={props.onTranscribe} onTranscript={(text) => props.onDraft(appendVoiceText(props.draft, text))} />
      </div>
      <div className="dc-manager-quick-help-actions"><small>{props.draft.length}/4000</small><button className="dc-button primary" disabled={busy || !props.draft.trim()} onClick={() => void props.onRequest(props.draft)}>{busy ? <><span className="dc-spinner" />Обрабатываем…</> : 'Отправить'}</button></div>
    </>}
    {props.error ? <p className="dc-manager-error" role="alert">{props.error}</p> : null}
    {props.job ? <ManagerJobProgress job={props.job} label="Подготовка ответа тренера" /> : null}
  </section>
}

function ManagerQuickHelpAnswer({ entry, onCopy, onEdit, onComplete, onBitrix }: {
  entry: ManagerQuickHelpEntry
  onCopy: (text: string, label: string) => Promise<void>
  onEdit: () => void
  onComplete: () => void
  onBitrix: () => void
}) {
  const content: ManagerQuickHelpContent = entry.content
  const [clientTone, setClientTone] = useState<ManagerQuickHelpContent['recommended_client_tone']>(content.recommended_client_tone)
  const [callTone, setCallTone] = useState<ManagerQuickHelpContent['recommended_call_tone']>(content.recommended_call_tone)
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
  return <article className="dc-manager-answer">
    <div className="dc-manager-answer-summary">
      <span>◎</span><div><h4>Понял ситуацию</h4><p><span>{content.situation_summary || 'Ситуация пока не сформирована.'}</span>{content.next_action ? <> <strong>{content.next_action}</strong></> : null}{content.expected_result ? <> <span> {content.expected_result}</span></> : null}</p></div><button className="dc-link-button" onClick={onEdit}>Изменить</button>
    </div>
    <div className="dc-manager-answer-modules">
      <section className="dc-manager-answer-copy message"><div><h4>Сообщение клиенту</h4><button className="dc-button" disabled={!clientMessage} onClick={() => void onCopy(clientMessage, 'Сообщение клиенту')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Тон сообщения клиенту">{clientTones.map(([tone, label]) => <button key={tone} type="button" role="tab" aria-selected={clientTone === tone} className={clientTone === tone ? 'active' : ''} onClick={() => setClientTone(tone)}><span>{label}</span>{content.recommended_client_tone === tone ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{clientMessage || 'Сообщение пока не сформировано.'}</pre></section>
      <section className="dc-manager-answer-copy speech"><div><h4>Речевой модуль</h4><button className="dc-button" disabled={!callScript} onClick={() => void onCopy(callScript, 'Речевой модуль')}>Скопировать</button></div><div className="dc-manager-tone-tabs" role="tablist" aria-label="Тон речевого модуля">{callTones.map(([tone, label]) => <button key={tone} type="button" role="tab" aria-selected={callTone === tone} className={callTone === tone ? 'active' : ''} onClick={() => setCallTone(tone)}><span>{label}</span>{content.recommended_call_tone === tone ? <small>Рекомендуется</small> : null}</button>)}</div><pre>{callScript || 'Речевой модуль пока не сформирован.'}</pre></section>
    </div>
    <div className="dc-manager-answer-actions"><button className="dc-button primary" onClick={onComplete}>Коммуникация выполнена</button><button className="dc-button" onClick={onBitrix}>Добавить комментарий в Bitrix24</button></div>
  </article>
}

function ManagerAssistantModal(props: {
  deal: DealControlDeal
  workspace: ManagerAssistantWorkspace
  draft: string
  error: string
  job: ManagerQuickHelpJob | null
  onDraft: (value: string) => void
  onRequest: (question: string) => Promise<void>
  onClose: () => void
  onEditSituation: () => void
  onCopy: (text: string, label: string) => Promise<void>
  onTranscribe: (audio: Blob) => Promise<string>
  onCompleteCommunication: (quickHelpId: number) => void
}) {
  const [view, setView] = useState<'answer' | 'history' | 'context'>('answer')
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const busy = Boolean(props.job && ['queued', 'running'].includes(props.job.status))
  const entries = [...props.workspace.entries].sort((first, second) => first.id - second.id)
  const latestEntry = entries.length ? entries[entries.length - 1] : null
  const task = primaryBitrixTaskOf(props.deal)
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

  async function send() {
    if (busy || !props.draft.trim()) return
    setView('answer')
    await props.onRequest(props.draft)
  }

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
    <section className="dc-manager-assistant-modal" role="dialog" aria-modal="true" aria-labelledby="manager-assistant-title">
      <aside className="dc-manager-assistant-sidebar">
        <div className="dc-manager-assistant-brand"><span>AI</span><div><strong>Помощник менеджера</strong><small>Работа по текущей сделке</small></div></div>
        <div className="dc-manager-assistant-deal"><small>Сделка</small><strong>{props.deal.title || `Сделка #${props.deal.deal_id}`}</strong><span>#{props.deal.deal_id} · {props.deal.stage_name || 'этап не указан'}<br />{task ? compactTaskText(task.subject) : 'Нет открытой задачи'}</span></div>
        <nav>
          <button className={view === 'answer' ? 'active' : ''} onClick={() => setView('answer')}><span>✦</span>Чат с ИИ</button>
          <button className={view === 'history' ? 'active' : ''} onClick={() => setView('history')}><span>↻</span>История</button>
          <button className={view === 'context' ? 'active' : ''} onClick={() => setView('context')}><span>i</span>Контекст сделки</button>
        </nav>
        <p className="dc-manager-assistant-context-status">Контекст сделки подгружен. Ответ учитывает этап, задачу и предыдущие коммуникации.</p>
      </aside>
      <main className="dc-manager-assistant-main">
        <header><div><h2 id="manager-assistant-title">Быстрая ИИ помощь менеджеру</h2><p>Сделка #{props.deal.deal_id} · текущая задача: {task ? compactTaskText(task.subject).toLowerCase() : 'не назначена'}</p></div><span>Контекст учтён</span><button onClick={props.onClose} aria-label="Закрыть">×</button></header>
        <div className="dc-manager-assistant-content">
          {view === 'answer' ? <section className="dc-manager-assistant-thread">
            {latestEntry ? <div className="dc-manager-assistant-turn" key={latestEntry.id}>
              <div className="dc-manager-assistant-user-message"><small>Ваш запрос</small><p>{latestEntry.question}</p></div>
              <ManagerQuickHelpAnswer
                entry={latestEntry}
                onCopy={props.onCopy}
                onEdit={props.onEditSituation}
                onComplete={() => complete(latestEntry)}
                onBitrix={() => void prepareBitrixComment(latestEntry)}
              />
            </div> : null}
            {busy ? <div className="dc-manager-assistant-typing" role="status"><span /><span /><span /><small>{props.job?.detail || 'Помощник готовит ответ'}</small></div> : null}
          </section> : null}
          {view === 'history' ? <section className="dc-manager-assistant-history"><h3>История работы по сделке</h3>{props.workspace.timeline.length ? <ol>{props.workspace.timeline.map((item) => <li key={item.id}><time>{dateTime(item.occurred_at)}</time><i /><p>{item.text}</p></li>)}</ol> : <p>История по сделке пока не сформирована.</p>}</section> : null}
          {view === 'context' ? <section className="dc-manager-assistant-context-grid">
            <div><small>Этап</small><strong>{props.workspace.context.stage || 'Не указан'}</strong></div>
            <div><small>Текущая задача</small><strong>{props.workspace.context.current_task || 'Нет открытой задачи'}</strong></div>
            <div><small>Последняя коммуникация</small><strong>{props.workspace.context.last_communication ? `${dateTime(props.workspace.context.last_communication.occurred_at)} · ${props.workspace.context.last_communication.text}` : 'Нет доступных данных'}</strong></div>
            <div><small>Главный риск</small><strong>{props.workspace.context.main_risk || 'Не выделен'}</strong></div>
          </section> : null}
        </div>
        <footer>
          <ManagerVoiceInput dealId={props.deal.deal_id} disabled={busy} onTranscribe={props.onTranscribe} onTranscript={(text) => props.onDraft(appendVoiceText(props.draft, text))} />
          <textarea ref={inputRef} value={props.draft} maxLength={4000} onChange={(event) => props.onDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} placeholder="Напишите, что произошло после коммуникации или что ещё нужно сделать..." aria-label="Продолжение диалога с помощником" />
          <button className="dc-button primary" disabled={busy || !props.draft.trim()} onClick={() => void send()}>{busy ? <span className="dc-spinner" /> : 'Отправить'}</button>
          {props.error ? <small className="dc-manager-error">{props.error}</small> : null}
        </footer>
      </main>
    </section>
  </div>, document.body)
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

function communicationDuration(seconds: number) {
  const safe = Math.max(0, Math.round(seconds || 0))
  const minutes = Math.floor(safe / 60)
  const remainder = safe % 60
  if (!minutes) return `${remainder} сек`
  return remainder ? `${minutes} мин ${remainder} сек` : `${minutes} мин`
}

function countLabel(value: number, one: string, few: string, many: string) {
  const normalized = Math.abs(Math.trunc(value)) % 100
  if (normalized >= 11 && normalized <= 14) return many
  const last = normalized % 10
  if (last === 1) return one
  if (last >= 2 && last <= 4) return few
  return many
}

function DailyCommunicationWidget({ summary }: { summary?: DealControlCommunicationsToday | null }) {
  const [open, setOpen] = useState(false)
  const completed = Math.max(0, summary?.completed || 0)
  const available = Boolean(summary?.available)
  const items = summary?.items || []
  const lastTouch = items.reduce((latest, item) => !latest || item.occurred_at > latest.occurred_at ? item : latest, items[0])
  const lastTouchTime = lastTouch
    ? formatMoscowDateTime(lastTouch.occurred_at, { hour: '2-digit', minute: '2-digit' }) || '—'
    : '—'
  const calls = Math.max(0, summary?.calls || 0)
  const messages = Math.max(0, summary?.messages || 0)
  return <section className={`dc-communication-widget ${available ? '' : 'unavailable'}`}>
    <div className="dc-communication-head">
      <div className="dc-communication-title"><span>↗</span><div><h3>Коммуникации сегодня</h3></div></div>
      <div className="dc-communication-score"><small>{available ? `${completed} ${countLabel(completed, 'касание', 'касания', 'касаний')}` : 'Нет данных'}</small></div>
    </div>
    <div className="dc-communication-stats">
      <div><strong>{calls}</strong><small>{countLabel(calls, 'звонок', 'звонка', 'звонков')}</small></div>
      <div><strong>{messages}</strong><small>{countLabel(messages, 'сообщение', 'сообщения', 'сообщений')}</small></div>
      <div><strong>{communicationDuration(summary?.duration_seconds || 0)}</strong><small>разговор</small></div>
      <div><strong>{lastTouchTime}</strong><small>последнее касание</small></div>
    </div>
    <div className="dc-communication-actions">
      {!available ? <p className="dc-communication-note">Обновите Bitrix, чтобы получить активности за текущий московский день.</p> : null}
      <button type="button" disabled={!items.length} aria-expanded={open} onClick={() => setOpen((value) => !value)}>{open ? 'Скрыть детали' : 'Показать детали'}</button>
    </div>
    {open && items.length ? <div className="dc-communication-details">
      {items.map((item) => {
        const call = item.channel === 'call'
        const time = formatMoscowDateTime(item.occurred_at, { hour: '2-digit', minute: '2-digit' }) || '—'
        const boundary = item.contact_class === 'confirmed_contact' ? 'контакт подтверждён' : 'результат клиента не подтверждён'
        return <article key={item.event_id}>
          <span>{call ? '☎' : '✉'}</span>
          <div><strong>{item.subject || (call ? 'Звонок' : 'Сообщение')}</strong><small>{time} · {boundary}</small></div>
          <b>{call ? communicationDuration(item.duration_seconds || 0) : 'текст'}</b>
        </article>
      })}
    </div> : null}
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

function CurrentTask(props: {
  view: DealControlView
  deal: DealControlDeal
  task: DealControlTask | null
  onConfirmMatch: (task: DealControlTask) => Promise<void>
  onReviewFact: (task: DealControlTask, factId: number, reviewStatus: 'confirmed' | 'rejected') => Promise<void>
  onReschedule: (task: DealControlTask) => void
  onPrepareManager: (task: DealControlTask) => Promise<void>
  onOutcome: (task: DealControlTask) => void
  guidanceJob: DealTaskGuidanceJob | null
  guidanceTaskId: number | null
  hasAnalysis: boolean
  onAdoptBitrixTask: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
  onToggleBitrixCompletion: (deal: DealControlDeal, task: DealControlBitrixTask) => Promise<void>
}) {
  const task = props.task
  const bitrixTask = primaryBitrixTaskOf(props.deal)
  const currentGuidance = task?.guidance && !task.guidance.is_stale
  const guidanceRunning = Boolean(
    task
    && props.guidanceTaskId === task.id
    && props.guidanceJob
    && ['queued', 'running'].includes(props.guidanceJob.status),
  )
  if (!task && bitrixTask) {
    return <>
      <BitrixTaskCard deal={props.deal} task={bitrixTask} onToggleCompletion={props.onToggleBitrixCompletion} />
      {props.deal.bitrix_tasks.length > 1 ? <details className="dc-bitrix-task-list">
        <summary>Другие задачи Bitrix: {props.deal.bitrix_tasks.length - 1}</summary>
        <ul>{props.deal.bitrix_tasks.slice(1).map((item) => <li key={item.activity_id}><strong>{compactTaskText(item.subject)}</strong><span>{dateTime(item.deadline)}</span></li>)}</ul>
      </details> : null}
    </>
  }
  return <section className={`dc-current-task ${task ? taskTone(task) : bitrixTask ? bitrixTaskTone(bitrixTask) : 'future'}`}>
    <div className="dc-section-head"><h3>{task ? 'Текущее поручение' : bitrixTask ? 'Текущая задача Bitrix' : 'Текущая задача'}</h3><ControlTimeChip task={task} bitrixTask={bitrixTask} /></div>
    {task ? <>
      <div className="dc-task-hero"><span>☎</span><div><h4>{compactTaskText(task.task_text)}</h4><p>{task.expected_result || 'Ожидаемый результат не указан'}</p></div></div>
      {compactTaskText(task.task_text) !== task.task_text.trim() ? <details className="dc-task-details"><summary>Подробное поручение</summary><p>{task.task_text}</p></details> : null}
      <p className="dc-task-meta">Срок: {dateTime(task.due_at)} · Касание: {task.touch_type || 'не указано'}</p>
      <ControlSyncState task={task} bitrixTask={bitrixTask} />
      <div className="dc-task-actions">
        {props.view === 'manager'
          ? <button className="dc-button primary" onClick={() => props.onOutcome(task)}>{task.latest_outcome ? 'Обновить результат' : 'Зафиксировать результат'}</button>
          : task.latest_outcome
            ? <button className="dc-button primary" onClick={() => props.onOutcome(task)}>Скорректировать результат</button>
            : null}
        <button className="dc-button" disabled={task.local_status !== 'active'} onClick={() => props.onReschedule(task)}>Перенести срок</button>
      </div>
      {task.latest_outcome ? <div className="dc-outcome-summary">
        <div><small>Контакт</small><strong>{CONTACT_LABELS[task.latest_outcome.contact_status]}</strong></div>
        <div><small>Результат</small><strong>{OUTCOME_LABELS[task.latest_outcome.result_status]}</strong></div>
        <div><small>Следующий шаг</small><strong>{task.latest_outcome.next_step_text || 'Не назначен'}</strong><span>{dateTime(task.latest_outcome.next_step_at)}</span></div>
      </div> : <p className="dc-boundary-note">{props.view === 'rop' ? 'Менеджер ещё не зафиксировал результат. Закрытие задачи в Bitrix само по себе не считается ответом клиента.' : 'Результат ещё не зафиксирован. Закрытие задачи в Bitrix само по себе не считается ответом клиента.'}</p>}
      {task.crm_facts?.length ? <details className="dc-crm-facts"><summary>Что обнаружено после обновления Bitrix: {task.crm_facts.length}</summary><ul>{task.crm_facts.map((fact) => <li key={fact.id}><span>{fact.review_status === 'confirmed' ? '✓' : fact.review_status === 'rejected' ? '×' : '?'}</span><div><strong>{fact.summary || fact.fact_kind}</strong><small>{dateTime(fact.occurred_at)} · {fact.contact_class === 'attempt' ? 'попытка, контакт не доказан' : fact.contact_class === 'deal_progress' ? 'движение сделки' : 'требует проверки'}</small>{fact.review_status === 'candidate' ? <em><button onClick={() => void props.onReviewFact(task, fact.id, 'confirmed')}>Подтвердить факт</button><button onClick={() => void props.onReviewFact(task, fact.id, 'rejected')}>Не относится</button></em> : null}</div></li>)}</ul></details> : null}
      {task.crm_execution_status === 'match_review' ? <button className="dc-link-button" onClick={() => void props.onConfirmMatch(task)}>Подтвердить совпадение с задачей Bitrix</button> : null}
      {props.view !== 'manager' ? <div className="dc-guidance-action">
        <div>
          <strong>{currentGuidance ? '✓ AI-подсказка готова' : task.guidance?.is_stale ? 'Подсказку нужно обновить' : 'Подготовка менеджера'}</strong>
          <small>{!props.hasAnalysis
            ? 'Сначала проведите полный анализ сделки'
            : currentGuidance
              ? 'Она соответствует текущей версии задачи и последнему анализу'
              : 'AI свяжет поручение с фактами сделки, вопросами и готовым текстом'}</small>
        </div>
        <button
          className="dc-button guidance"
          disabled={!props.hasAnalysis || task.local_status !== 'active' || guidanceRunning}
          onClick={() => void props.onPrepareManager(task)}
        >
          {guidanceRunning
            ? <><span className="dc-spinner" />Готовим…</>
            : currentGuidance
              ? 'Обновить подсказку'
              : task.guidance?.is_stale
                ? 'Обновить подсказку'
                : '✦ Подготовить менеджера'}
        </button>
      </div> : null}
      {props.guidanceJob && props.guidanceTaskId === task.id
        ? <TaskGuidanceProgress job={props.guidanceJob} />
        : null}
      <small className="dc-boundary-note">Выполнение поручения и результат по клиенту учитываются отдельно.</small>
    </> : <div className="dc-missing-task-state"><strong>В B24 нет открытой задачи</strong><p>Это критичное состояние: по сделке не назначен следующий контролируемый шаг.</p><a className="dc-button" href={bitrixDealUrl(props.deal.deal_id)} target="_blank" rel="noreferrer">Открыть сделку в B24 ↗</a></div>}
  </section>
}

function TaskGuidanceProgress({ job }: { job: DealTaskGuidanceJob }) {
  const error = job.status === 'error'
  const done = job.status === 'done'
  return <div className={`dc-guidance-progress ${error ? 'error' : done ? 'done' : ''}`} role="status" aria-live="polite">
    <div><strong>{error ? 'Не удалось подготовить подсказку' : done ? 'Подсказка готова' : job.detail}</strong><b>{job.percent}%</b></div>
    <span><i style={{ width: `${job.percent}%` }} /></span>
    {error ? <small>{job.error || 'Повторите запуск после проверки ошибки.'}</small> : null}
  </div>
}

function RopGuidance({ deal, task, onCopy }: {
  deal: DealControlDeal
  task: DealControlTask | null
  onCopy: (text: string, label: string) => Promise<void>
}) {
  const coaching = deal.coaching
  const guidance = task?.guidance && !task.guidance.is_stale ? task.guidance.content : null
  return <>
    {guidance ? <TaskGuidanceContent content={guidance} touchType={task?.touch_type} onCopy={onCopy} ropPreview /> : null}
    <section className="dc-analysis-section">
      <div className="dc-section-head"><h3>Сильные и слабые стороны</h3></div>
      <div className="dc-two-columns">
        <ListCard tone="good" title="✓ Сильные стороны" items={coaching.strengths} empty="Подтверждённые сильные стороны не выделены." />
        <ListCard tone="weak" title="✕ Слабые стороны" items={coaching.weaknesses} empty="Риски и пробелы не выделены." />
      </div>
    </section>
    <section className="dc-text-section"><div className="dc-section-head"><h3>Сообщение менеджеру</h3></div><div className="dc-coaching-copy"><strong>Готово к отправке</strong><p>{textOr(coaching.manager_coaching, 'В анализе нет готового сообщения менеджеру.')}</p></div><div className="dc-copy-actions"><button className="dc-button primary" disabled={!coaching.manager_coaching} onClick={() => void onCopy(coaching.manager_coaching || '', 'Текст для менеджера')}>Скопировать менеджеру</button></div></section>
  </>
}

function TaskGuidanceContent({ content, touchType, onCopy, ropPreview = false }: {
  content: DealTaskGuidanceContent
  touchType?: string | null
  onCopy: (text: string, label: string) => Promise<void>
  ropPreview?: boolean
}) {
  const isCall = !touchType || touchType.toLocaleLowerCase('ru').includes('звон')
  return <>
    <section className="dc-analysis-section dc-task-guidance">
      <div className="dc-section-head"><h3>{ropPreview ? 'AI-подготовка менеджера' : 'Что известно и чего не хватает'}</h3><span>✦ По текущей задаче</span></div>
      {ropPreview ? <div className="dc-guidance-summary"><strong>Фокус задачи</strong><p>{content.task_focus}</p><small>Ожидаемый результат: {content.expected_outcome}</small></div> : null}
      <div className="dc-two-columns">
        <ListCard tone="good" title="✓ Уже известно" items={content.known_facts} empty="Подтверждённые факты не выделены." />
        <ListCard tone="weak" title="✕ Нужно выяснить" items={content.missing_facts} empty="Дополнительные факты выяснять не требуется." />
      </div>
    </section>
    <section className="dc-manager-module dc-task-guidance">
      <div className="dc-section-head"><div><h3>Как выполнить задачу РОПа</h3><p>Подсказка относится только к текущему поручению</p></div><span>✦ Помощник продаж</span></div>
      <div className="dc-contact-goal"><strong>Цель текущего контакта</strong><p>{content.contact_goal}</p></div>
      <div className="dc-question-list"><strong>Что обязательно выяснить</strong>{content.contact_questions.length ? <ol>{content.contact_questions.map((item) => <li key={item}>{item}</li>)}</ol> : <p>Дополнительные вопросы не требуются.</p>}</div>
      <div className="dc-script"><strong>{isCall ? 'Речевой модуль для звонка' : 'Готовый текст для клиента'}</strong><pre>{content.ready_text}</pre><div><button className="dc-button primary" onClick={() => void onCopy(content.ready_text, isCall ? 'Сценарий звонка' : 'Текст сообщения')}>Скопировать {isCall ? 'сценарий звонка' : 'текст сообщения'}</button></div></div>
      <div className="dc-crm-checklist"><strong>Что зафиксировать в Bitrix после контакта</strong><ul>{content.crm_checklist.map((item) => <li key={item}>{item}</li>)}</ul></div>
    </section>
  </>
}

function ListCard({ tone, title, items, empty }: { tone: 'good' | 'weak'; title: string; items: string[]; empty: string }) {
  return <article className={tone}><strong>{title}</strong><ul>{items.map((item) => <li key={item}>{item}</li>)}{!items.length ? <li>{empty}</li> : null}</ul></article>
}

function TaskEditor(props: {
  taskText: string
  setTaskText: (value: string) => void
  touchType: string
  setTouchType: (value: string) => void
  expectedResult: string
  setExpectedResult: (value: string) => void
  dueAt: string
  setDueAt: (value: string) => void
  hint?: string
  expectedHint?: string
  onAddTask: () => Promise<void>
}) {
  return <details className="dc-task-editor">
    <summary>Поставить новое поручение менеджеру</summary>
    <label>Что нужно сделать<textarea value={props.taskText} onChange={(event) => props.setTaskText(event.target.value)} placeholder={props.hint || 'Конкретное действие менеджера'} /></label>
    <div><label>Касание<select value={props.touchType} onChange={(event) => props.setTouchType(event.target.value)}><option>Звонок</option><option>Email</option><option>Мессенджер</option><option>CRM-задача</option><option>Другое</option></select></label><label>Срок<input type="datetime-local" value={props.dueAt} onChange={(event) => props.setDueAt(event.target.value)} /></label></div>
    <label>Какой результат должен появиться<input value={props.expectedResult} onChange={(event) => props.setExpectedResult(event.target.value)} placeholder={props.expectedHint || 'Факт клиента или следующий шаг в CRM'} /></label>
    <button className="dc-button primary" disabled={!props.taskText.trim() || !props.dueAt} onClick={() => void props.onAddTask()}>Сохранить поручение</button>
    <small>Приложение хранит поручение локально. В Bitrix его нужно создать вручную.</small>
  </details>
}
