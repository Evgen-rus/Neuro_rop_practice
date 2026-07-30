import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import {
  confirmDealControlTaskCrmMatch,
  createDealControlTask,
  fetchDealControl,
  fetchDealTaskGuidanceJob,
  fetchJob,
  recordDealControlTaskEvent,
  reviewDealControlCrmFact,
  saveDealControlScope,
  saveDealControlTaskOutcome,
  startAnalyze,
  startDealTaskGuidance,
  syncDealControl,
  updateDealControlDeal,
  updateDealControlBitrixTaskCompletion,
  updateDealControlTask,
  type DealControlDashboard,
  type DealControlBitrixTask,
  type DealControlDeal,
  type DealControlTask,
  type DealControlTaskOutcome,
  type DealTaskGuidanceContent,
  type DealTaskGuidanceJob,
  type JobState,
} from './api'

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
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parsed)
}

function dateTimeParts(value?: string | null) {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return { date: value, time: '' }
  return {
    date: new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit' }).format(parsed),
    time: new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' }).format(parsed),
  }
}

function dateOnly(value?: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  }).format(parsed)
}

function stageAge(value?: string | null) {
  if (!value) return null
  const parsed = new Date(value)
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

export function DealControl({ onExit }: { onExit?: () => void }) {
  const [data, setData] = useState<DealControlDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
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
          setNotice(`Анализ сделки #${analyzingDealId} завершён. Карточка обновлена.`)
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

  async function analyzeDeal(deal: DealControlDeal) {
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
        confirm_paid: false,
        transcript_mode: 'all',
      })
      setAnalysisJob(started)
      setNotice(`Анализ сделки #${deal.deal_id} запущен. Можно следить за этапами в карточке.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
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
        <div><h1>{copyForView.title}</h1><p>{copyForView.subtitle}</p></div>
        <div className="dc-refresh">
          <span>Обновлено {dateTime(data.generated_at)}</span>
          <button className="dc-button" disabled={syncing} onClick={() => void sync()}>
            {syncing ? <><span className="dc-spinner" />Обновляем Bitrix…</> : <><span>⟳</span>Обновить Bitrix</>}
          </button>
        </div>
      </header>

      {error ? <div className="dc-alert error">{error}</div> : null}
      {notice ? <div className="dc-alert success">{notice}</div> : null}
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
            <div><h2>{view === 'dashboard' ? 'Обзор портфеля сделок' : taskPlanTitle(timeView)}</h2><p>{view === 'dashboard' ? 'Каждая строка показывает стадию, контроль, прогноз оплаты и следующий шаг.' : 'Задачи выбранного периода, отсортированные по сроку.'}</p></div>
            <span>{view === 'dashboard' ? 'Сначала критичные ›' : 'Фокус дня ›'}</span>
          </div>
          {view === 'dashboard'
            ? <DealTable deals={visibleDeals} selectedId={selected?.deal_id || ''} onSelect={setSelectedId} onSaveFields={saveFields} onReschedule={beginReschedule} />
            : <TaskTable deals={visibleDeals} selectedId={selected?.deal_id || ''} onSelect={setSelectedId} />
          }
        </section>

        <div className="dc-resizer" onPointerDown={(event) => { event.preventDefault(); setDragging(true) }} title="Потяните, чтобы изменить ширину">⋮</div>

        <DealDetail
          view={view}
          deal={selected}
          onSaveFields={saveFields}
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
        ['◇', 'Без задачи в B24', summary.tasks_missing, 'blue'],
        ['◷', 'Просрочено', summary.tasks_overdue, 'red'],
        ['▣', 'На сегодня', summary.tasks_today, 'blue'],
        ['▤', 'На завтра', summary.tasks_tomorrow, 'orange'],
        ['✓', 'Выполнено сегодня', `${summary.tasks_completed_today} из ${summary.tasks_plan_today}`, 'green'],
      ]
  return <section className="dc-kpis">
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
    <span>{props.view === 'dashboard' ? 'Табличный обзор и детализация справа' : 'Просроченные задачи всегда выше остальных'}</span>
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
  return <div className="dc-table-wrap">
    <div className="dc-table-scroll">
      <div className="dc-deal-columns"><span>Сделка</span><span>Дата и время контроля</span><span>Стадия</span><span>Сумма и прогноз оплаты</span></div>
      {props.deals.map((deal) => {
        const task = currentTaskOf(deal)
        const bitrixTask = primaryBitrixTaskOf(deal)
        const age = stageAge(deal.modified_at_crm)
        return <article className={`dc-deal-row ${task ? taskTone(task) : bitrixTask ? bitrixTaskTone(bitrixTask) : 'future'} ${props.selectedId === deal.deal_id ? 'selected' : ''}`} key={deal.deal_id} onClick={() => props.onSelect(deal.deal_id)}>
          <div className="dc-deal-main"><small>Сделка</small><strong>{deal.title || `Сделка #${deal.deal_id}`}</strong><p><span className="dc-deal-id">#{deal.deal_id}</span><span>♟ {deal.manager_name || 'Не назначен'}</span><span>Создана {dateOnly(deal.created_at_crm)}</span></p></div>
          <div className="dc-control-cell"><small>Контроль</small><strong>{dateTime(task?.due_at || bitrixTask?.deadline || deal.next_control_at)}</strong><div><ControlTimeChip task={task} bitrixTask={bitrixTask} /><button disabled={!task} onClick={(event) => { event.stopPropagation(); if (task) props.onReschedule(task) }}>Переставить</button></div></div>
          <div className="dc-stage-cell"><small>Текущая стадия</small><strong>{deal.stage_name || 'Не указана'}</strong><p>Сделка обновлена {dateOnly(deal.modified_at_crm)}{age == null ? null : <span className={age > 30 ? 'danger' : age > 14 ? 'warn' : ''}>{age} дн.</span>}</p></div>
          <div className="dc-forecast-cell" onClick={(event) => event.stopPropagation()}><small>Сумма договора</small><strong>{money(deal.amount, deal.currency_id || 'RUB')}</strong><div>
            <select value={deal.probability ?? ''} onChange={(event) => void props.onSaveFields(deal, { probability: event.target.value ? Number(event.target.value) : null })}><option value="">—%</option>{[0, 10, 25, 50, 60, 70, 80, 100].map((value) => <option value={value} key={value}>{value}%</option>)}</select>
            <input defaultValue={deal.expected_payment_period || ''} onBlur={(event) => void props.onSaveFields(deal, { expected_payment_period: event.target.value || null })} placeholder="Неделя / месяц" />
          </div></div>
        </article>
      })}
      {!props.deals.length ? <p className="dc-empty">В выбранном разделе сделок нет.</p> : null}
    </div>
  </div>
}

function TaskTable({ deals, selectedId, onSelect }: { deals: DealControlDeal[]; selectedId: string; onSelect: (id: string) => void }) {
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
          <div><span className="dc-stage-pill">{deal.stage_name || 'Не указана'}</span><small>♟ {deal.manager_name}</small></div>
          <div className={`dc-task-name ${bitrixTask ? '' : 'missing'}`}><strong>{bitrixTask ? compactTaskText(bitrixTask.subject).replace(/^CRM:\s*/i, '') : 'В B24 нет открытой задачи'}</strong></div>
          <div className="dc-task-deadline-cell"><time className="dc-task-deadline">{deadline ? <><strong>{deadline.date}</strong>{deadline.time ? <span>{deadline.time}</span> : null}</> : <span>Не назначен</span>}</time></div>
          <div className="dc-task-result-cell"><ControlTimeChip task={null} bitrixTask={bitrixTask} />{bitrixTask?.completion_state === 'local' ? <small>В B24 ещё открыта</small> : bitrixTask?.completion_state === 'bitrix' ? <small>Подтверждено в B24</small> : null}</div>
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

function DealDetail(props: {
  view: DealControlView
  deal: DealControlDeal | null
  onSaveFields: (
    deal: DealControlDeal,
    patch: Partial<Pick<DealControlDeal, 'probability' | 'expected_payment_period' | 'next_control_at'>>,
  ) => Promise<void>
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
  analysisJob: JobState | null
  analyzingDealId: string
  onAnalyze: (deal: DealControlDeal) => Promise<void>
  guidanceJob: DealTaskGuidanceJob | null
  guidanceTaskId: number | null
}) {
  if (!props.deal) return <aside className="dc-detail"><p className="dc-empty">Выберите сделку в таблице.</p></aside>
  const deal = props.deal
  const task = currentTaskOf(deal)
  const coaching = deal.coaching
  const managerView = props.view === 'manager'
  const taskGuidance = task?.guidance && !task.guidance.is_stale ? task.guidance.content : null
  const managerFocus = task
    ? taskGuidance?.contact_goal || coaching.contact_goal || 'Выполнить поручение РОПа и зафиксировать подтверждённый результат.'
    : coaching.contact_goal

  return <aside className="dc-detail">
    <header>
      <div><h2>Сделка #{deal.deal_id}</h2><p>{deal.title}</p><a className="dc-button primary dc-bitrix-detail-link" href={bitrixDealUrl(deal.deal_id)} target="_blank" rel="noreferrer">Открыть сделку в Bitrix ↗</a></div>
      <button
        className="dc-button dc-analyze-button"
        disabled={Boolean(props.analysisJob && ['queued', 'running'].includes(props.analysisJob.status))}
        onClick={() => void props.onAnalyze(deal)}
      >
        {props.analysisJob && props.analyzingDealId === deal.deal_id && ['queued', 'running'].includes(props.analysisJob.status)
          ? <><span className="dc-spinner" />Анализируем…</>
          : <><span>✦</span>{coaching.report_id ? 'Обновить анализ' : 'Провести анализ'}</>}
      </button>
    </header>
    {props.analysisJob && props.analyzingDealId === deal.deal_id
      ? <DealAnalysisProgress job={props.analysisJob} dealId={deal.deal_id} />
      : null}
    <section className="dc-detail-stats">
      <div><small>Этап</small><strong>{deal.stage_name || '—'}</strong></div>
      <div><small>Вероятность</small><strong>{deal.probability == null ? '—' : `${deal.probability}%`}</strong></div>
      <div><small>{managerView ? 'Создана' : 'Менеджер'}</small><strong>{managerView ? dateOnly(deal.created_at_crm) : deal.manager_name || '—'}</strong></div>
      <div><small>Сумма</small><strong>{money(deal.amount, deal.currency_id || 'RUB')}</strong></div>
    </section>

    <section className="dc-insight-card">
      <div className="dc-section-head"><h3>Текущая ситуация</h3><span>✦ AI-сводка</span></div>
      <p>{textOr(coaching.current_situation, 'По сделке ещё нет сохранённого полного анализа. CRM-поля и локальные задачи доступны, аналитические выводы появятся после анализа.')}</p>
      <div className="dc-mini-grid">
        <div><small>Фокус РОПа</small><strong>{textOr(managerView ? managerFocus : coaching.rop_focus, 'Не сформирован')}</strong></div>
      </div>
    </section>

    <CurrentTask
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
      hasAnalysis={Boolean(coaching.report_id)}
      onAdoptBitrixTask={props.onAdoptBitrixTask}
      onToggleBitrixCompletion={props.onToggleBitrixCompletion}
    />

    {managerView
      ? <ManagerGuidance deal={deal} task={task} onCopy={props.onCopy} />
      : <RopGuidance deal={deal} task={task} onCopy={props.onCopy} />
    }

    {props.view === 'dashboard' ? <section className="dc-manual-fields">
      <div className="dc-section-head"><h3>Ручной прогноз и контроль</h3><span>Локально</span></div>
      <label>Вероятность<select value={deal.probability ?? ''} onChange={(event) => void props.onSaveFields(deal, { probability: event.target.value ? Number(event.target.value) : null })}><option value="">Не указана</option>{[0, 10, 25, 50, 60, 70, 80, 100].map((value) => <option key={value} value={value}>{value}%</option>)}</select></label>
      <label>Неделя / месяц оплаты<input defaultValue={deal.expected_payment_period || ''} onBlur={(event) => void props.onSaveFields(deal, { expected_payment_period: event.target.value || null })} /></label>
      <label>Следующий контроль<input type="datetime-local" value={(deal.next_control_at || '').slice(0, 16)} onChange={(event) => void props.onSaveFields(deal, { next_control_at: event.target.value || null })} /></label>
    </section> : null}

    {props.view !== 'manager' && deal.current_task ? <TaskEditor
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
  </aside>
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
    </> : bitrixTask ? <>
      <div className="dc-task-hero"><span>☎</span><div><h4>{compactTaskText(bitrixTask.subject).replace(/^CRM:\s*/i, '')}</h4>{bitrixTask.description ? <p>{compactTaskText(bitrixTask.description, 220)}</p> : null}</div></div>
      <p className="dc-task-meta">Срок: {dateTime(bitrixTask.deadline)} · Этап: {props.deal.stage_name || 'не указан'}</p>
      {bitrixTask.completion_state === 'local'
        ? <p className="dc-bitrix-completion-note">Выполнено в приложении · В B24 ещё открыта</p>
        : bitrixTask.completion_state === 'bitrix'
          ? <p className="dc-bitrix-completion-note confirmed">Выполнение подтверждено в B24</p>
          : null}
      <div className="dc-task-actions">
        {bitrixTask.completion_state === 'bitrix'
          ? <button className="dc-button primary" disabled>Выполнено в B24</button>
          : <button className={`dc-button ${bitrixTask.completion_state === 'local' ? '' : 'primary'}`} onClick={() => void props.onToggleBitrixCompletion(props.deal, bitrixTask)}>
              {bitrixTask.completion_state === 'local' ? 'Вернуть в работу' : 'Отметить выполненной'}
            </button>}
        {bitrixTaskUrl(bitrixTask)
          ? <a className="dc-button dc-task-link" href={bitrixTaskUrl(bitrixTask) || undefined} target="_blank" rel="noreferrer">Открыть задачу в B24 ↗</a>
          : <button className="dc-button" disabled title="Bitrix не передал ID связанной задачи">Открыть задачу в B24</button>}
      </div>
      {props.deal.bitrix_tasks.length > 1 ? <details className="dc-bitrix-task-list">
        <summary>Другие задачи Bitrix: {props.deal.bitrix_tasks.length - 1}</summary>
        <ul>{props.deal.bitrix_tasks.slice(1).map((item) => <li key={item.activity_id}><strong>{compactTaskText(item.subject)}</strong><span>{dateTime(item.deadline)}</span></li>)}</ul>
      </details> : null}
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
      <div className="dc-section-head"><h3>Сильные и слабые стороны</h3><span>✦ Анализ нейросети</span></div>
      <div className="dc-two-columns">
        <ListCard tone="good" title="✓ Сильные стороны" items={coaching.strengths} empty="Подтверждённые сильные стороны не выделены." />
        <ListCard tone="weak" title="✕ Слабые стороны" items={coaching.weaknesses} empty="Риски и пробелы не выделены." />
      </div>
    </section>
    <section className="dc-text-section"><div className="dc-section-head"><h3>Актуализация и текущая логика сделки</h3><span>✦ Вывод</span></div><p><strong>Текущая ситуация.</strong> {textOr(coaching.current_situation, 'Анализ пока не сохранён.')}</p><p><strong>Что проверить прямо сейчас.</strong> {textOr(coaching.what_to_check_now, 'Проверь актуальность задачи, наличие клиентского ответа и следующего шага.')}</p></section>
    <section className="dc-text-section"><div className="dc-section-head"><h3>Разбор для менеджера</h3><span>Коучинг</span></div><div className="dc-coaching-copy"><strong>Формулировка для разговора с менеджером</strong><p>{textOr(coaching.manager_coaching, 'Готовый разбор появится после полного анализа сделки.')}</p></div><div className="dc-copy-actions"><button className="dc-button" onClick={() => void onCopy(coaching.manager_coaching || '', 'Разбор')}>Скопировать разбор</button><button className="dc-button primary" onClick={() => void onCopy(coaching.rop_task_hint || coaching.manager_coaching || '', 'Поручение менеджеру')}>Скопировать поручение</button></div></section>
  </>
}

function ManagerGuidance({ deal, task, onCopy }: {
  deal: DealControlDeal
  task: DealControlTask | null
  onCopy: (text: string, label: string) => Promise<void>
}) {
  const coaching = deal.coaching
  if (task?.guidance && !task.guidance.is_stale) {
    return <TaskGuidanceContent content={task.guidance.content} touchType={task.touch_type} onCopy={onCopy} />
  }
  return <>
    {task ? <section className="dc-guidance-missing">
        <span>✦</span>
        <div>
          <h3>{task.guidance?.is_stale ? 'AI-подсказка устарела' : 'Подсказка к задаче ещё не подготовлена'}</h3>
          <p>{task.guidance?.is_stale
            ? 'Задача или анализ сделки изменились. Ниже показана базовая подготовка из последнего полного анализа; она не заменяет подсказку к текущей задаче.'
            : 'Ниже показана базовая подготовка из последнего полного анализа сделки. РОП может отдельно подготовить подсказку именно к текущей задаче.'}</p>
        </div>
      </section> : null}
    <section className="dc-analysis-section">
      <div className="dc-section-head"><h3>Что известно и чего не хватает</h3><span>✦ Последний полный анализ</span></div>
      <div className="dc-two-columns">
        <ListCard tone="good" title="✓ Уже известно" items={coaching.known} empty="Подтверждённые факты пока не выделены." />
        <ListCard tone="weak" title="✕ Нужно выяснить" items={coaching.unknowns} empty="Дополнительные вопросы не выделены." />
      </div>
    </section>
    <section className="dc-manager-module">
      <div className="dc-section-head"><div><h3>Как проработать сделку</h3><p>Открой перед звонком и двигайся по шагам</p></div><span>✦ Помощник продаж</span></div>
      <div className="dc-contact-goal"><strong>Цель текущего контакта</strong><p>{textOr(coaching.contact_goal, 'Выполнить поручение РОПа и получить конкретный подтверждённый результат.')}</p></div>
      <div className="dc-question-list"><strong>Что обязательно выяснить</strong>{coaching.questions.length ? <ol>{coaching.questions.map((item) => <li key={item}>{item}</li>)}</ol> : <p>Вопросы появятся после полного анализа сделки.</p>}</div>
      <div className="dc-script"><strong>Речевой модуль для звонка</strong><pre>{textOr(coaching.script, 'Готовый скрипт пока не сформирован.')}</pre><div><button className="dc-button primary" onClick={() => void onCopy(coaching.script || '', 'Сценарий звонка')}>Скопировать сценарий звонка</button></div></div>
    </section>
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
