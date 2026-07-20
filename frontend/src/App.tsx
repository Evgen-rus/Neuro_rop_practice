import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import './index.css'
import {
  asRecord,
  asString,
  asStringList,
  unwrapAnalysis,
  type AnalyzeOptions,
  type AnalysisProfile,
  type Candidate,
  type CandidateFilter,
  type CandidatesResponse,
  type CrmPipeline,
  type JobState,
  type DailyPreview,
  type DailySummaryRun,
  type EntityProgress,
  type UiReportDetail,
  type UiReportListItem,
  type LeadWorkflowState,
  createAnalysisProfile,
  createDailySummary,
  deleteAnalysisProfile,
  fetchAnalysisProfiles,
  fetchCandidateFilter,
  fetchDailySummary,
  fetchCompactEvidence,
  fetchCompactJob,
  fetchCompactReview,
  fetchJob,
  fetchPipelines,
  fetchReport,
  fetchReportMarkdown,
  fetchReports,
  previewAnalysisProfile,
  saveDecision,
  saveLeadWorkflow,
  saveCompactFeedback,
  saveOutcome,
  saveQualificationReview,
  searchCandidates,
  selectAnalysisProfile,
  startDailySummary,
  startAnalyze,
  startCompactRun,
  updateAnalysisProfile,
  type CompactReview,
} from './api'

type Tab = 'summary' | 'dashboard' | 'manual' | 'history'

type ManualEntityType = 'lead' | 'deal' | 'auto'
type CandidateReviewView = 'active' | 'reviewed' | 'all'

type ManualInput = {
  ids: string[]
  entityType: ManualEntityType
  detected: Array<{ entityType: 'lead' | 'deal'; id: string }>
  error: string | null
}

const DECISIONS = [
  'Подтвердить рекомендацию',
  'Поставить задачу менеджеру',
  'Вернуть в контроль',
  'Проверить через 2 дня',
  'Закрытие обосновано',
  'Недостаточно данных',
]

function dateInTimeZone(timeZone: string): string {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${value.year}-${value.month}-${value.day}`
}

const OUTCOMES = [
  'задача выполнена',
  'задача не выполнена',
  'клиент ответил',
  'клиент не ответил',
  'сделка возвращена в работу',
  'рекомендация ошибочная',
  'нужна повторная проверка',
]

const RISK_LEVEL_RU: Record<string, string> = {
  high: 'Высокий',
  medium_high: 'Средне-высокий',
  medium: 'Средний',
  low: 'Низкий',
}

const VERDICT_RU: Record<string, string> = {
  bad_processing: 'плохая обработка',
  bad_lead: 'плохой лид',
  data_gap: 'не хватает данных',
  needs_nurture: 'нужен прогрев',
  ready_for_deal: 'готов к сделке',
  technical_mismatch: 'техническое несоответствие',
  budget_below_new_equipment_minimum: 'бюджет ниже минимума для нового оборудования',
  unknown: 'неясно',
}

const LLM_REQUIRED_FRESHNESS = new Set(['missing', 'changed', 'failed'])

const PROGRESS_STAGE_RU: Record<string, string> = {
  queued: 'В очереди',
  crm_context: 'Собирается история CRM',
  audio_lookup: 'Ищутся звонки',
  audio_download: 'Загружается аудио',
  transcription: 'Транскрибируется аудио',
  llm_analysis: 'Выполняется LLM-анализ',
  validation: 'Проверяется ответ модели',
  report: 'Формируется отчёт',
  done: 'Готово',
  error: 'Ошибка',
  skipped: 'Не запущено',
}

function candidateNeedsLlm(candidate: Candidate) {
  return LLM_REQUIRED_FRESHNESS.has(candidate.analysis_freshness || 'missing')
}

function ActivitySpinner() {
  return <span className="activity-spinner" aria-hidden="true" />
}

function EntityProgressView({ progress }: { progress: Partial<EntityProgress> }) {
  const stage = progress.stage || 'queued'
  const status = progress.status || 'queued'
  const current = Number(progress.current || 0)
  const total = Number(progress.total || 0)
  const attempt = Number(progress.attempt || 0)
  const maxAttempts = Number(progress.max_attempts || 0)
  const started = progress.started_at ? Date.parse(progress.started_at) : Number.NaN
  const elapsedSeconds = Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : null
  const elapsed = elapsedSeconds === null ? null : `${String(Math.floor(elapsedSeconds / 60)).padStart(2, '0')}:${String(elapsedSeconds % 60).padStart(2, '0')}`
  const active = status === 'queued' || status === 'running'
  return <div className={`entity-progress ${status}`}>
    <div className="entity-progress-head">
      <strong className="progress-stage-label">{active ? <ActivitySpinner /> : null}{PROGRESS_STAGE_RU[stage] || stage}</strong>
      {elapsed ? <span>{elapsed}</span> : null}
    </div>
    {progress.detail ? <p>{progress.detail}</p> : null}
    <div className="entity-progress-meta">
      {total > 0 ? <span>{current} из {total}</span> : null}
      {maxAttempts > 1 && attempt > 0 ? <span>Попытка {attempt} из {maxAttempts}</span> : null}
      {progress.updated_at ? <span>Обновлено {new Date(progress.updated_at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span> : null}
    </div>
    {progress.error ? <div className="entity-progress-error">{progress.error}</div> : null}
  </div>
}

const BANT_STATUS_RU: Record<string, string> = {
  confirmed: 'подтверждён',
  incomplete: 'неполный',
  missing: 'не хватает данных',
  not_confirmed: 'не подтверждён',
  negative: 'подтверждён стоп-фактор',
  unknown: 'нет данных',
}

const BANT_SHORT_LABELS: Record<string, string> = {
  budget: 'B',
  authority: 'A',
  need: 'N',
  timeframe: 'T',
}

const QUALIFICATION_ISSUE_LABELS: Record<string, string> = {
  budget: 'Budget',
  authority: 'Authority',
  need: 'Need',
  timeframe: 'Timeframe',
  category: 'Категория клиента',
  solution_fit: 'Техническая применимость',
  commercial_fit: 'Бюджетный стоп-фактор',
}

const BANT_FILTER_RU: Record<string, string> = {
  complete: 'полный',
  incomplete: 'неполный',
  budget: 'не подтверждён Budget',
  authority: 'не подтверждён Authority',
  need: 'не подтверждён Need',
  timeframe: 'не подтверждён Timeframe',
  negative: 'есть отрицательный ответ',
  unknown: 'есть критерий без данных',
}

function LeadQualificationStrip({
  summary,
  hasAnalysis = false,
  category,
}: {
  summary?: Candidate['lead_qualification'] | null
  hasAnalysis?: boolean
  category?: string | null
}) {
  if (!summary) {
    return (
      <div className="lead-qualification-strip empty">
        {hasAnalysis ? 'BANT: нет структурированных данных в старом анализе' : 'BANT: анализ не выполнен'} · Категория: {category || 'не определена'}
      </div>
    )
  }
  return (
    <div className="lead-qualification-strip">
      <span className="lead-category-chip">Категория {summary.category === 'unknown' ? 'Unknown' : summary.category}</span>
      <span className="bant-count-chip">BANT {summary.confirmed_count} из {summary.total_count || 4}</span>
      <span className="bant-mini-statuses">
        {Object.entries(BANT_SHORT_LABELS).map(([key, label]) => {
          const status = summary.statuses?.[key as keyof typeof summary.statuses] || 'unknown'
          return <span className={`bant-mini status-${status}`} title={`${label}: ${BANT_STATUS_RU[status] || status}`} key={key}>{label}{status === 'confirmed' ? '✓' : status === 'negative' ? '×' : '?'}</span>
        })}
      </span>
      {summary.category === 'C' ? <span className={`summary-return status-${summary.controlled_return_status || 'unknown'}`}>
        Возврат: {assessmentLabel(summary.controlled_return_status || 'unknown', CONTROLLED_RETURN_STATUS_RU)}
        {summary.controlled_return_date ? ` · CRM ${summary.controlled_return_date}` : summary.recommended_return_date ? ` · рекомендуется ${summary.recommended_return_date}` : ''}
      </span> : null}
    </div>
  )
}

const LEAD_ROUTE_STATUS_RU: Record<string, string> = {
  allowed: 'маршрут допустим',
  violation: 'нарушение маршрута',
  needs_clarification: 'маршрут нужно уточнить',
  unknown: 'маршрут не определён',
}

const LEAD_ROUTE_RU: Record<string, string> = {
  ordinary_deal: 'Обычная сделка',
  op2: 'ОП2',
  clarification: 'Довыяснение',
  auto_reminder: 'Автонапоминание',
  deferred_demand: 'Отложенный спрос',
  disqualified: 'Дисквалификация',
  unknown: 'Не определён',
}

const CONTROLLED_RETURN_STATUS_RU: Record<string, string> = {
  confirmed_in_crm: 'подтверждён в CRM',
  missing_in_crm: 'действие в CRM отсутствует',
  needs_clarification: 'нужно проверить CRM',
  not_required: 'не требуется',
}

const SOLUTION_FIT_STATUS_RU: Record<string, string> = {
  compatible: 'совместимо',
  not_compatible: 'не совместимо',
  needs_technical_data: 'нужны параметры',
  unknown: 'нет данных',
}

const COMMERCIAL_FIT_STATUS_RU: Record<string, string> = {
  sufficient: 'достаточен',
  below_minimum: 'ниже минимума',
  unknown: 'нет данных',
}

function riskLabelRu(value: string): string {
  const key = value.trim().toLowerCase()
  return RISK_LEVEL_RU[key] || value || '—'
}

function priorityLabelRu(value: string): string {
  const key = value.trim().toLowerCase()
  if (key === 'high') return 'Высокий риск'
  if (key === 'medium') return 'Средний риск'
  if (key === 'low') return 'Низкий риск'
  return value || 'Риск не указан'
}

function verdictLabelRu(value: string): string {
  const key = value.trim().toLowerCase()
  return VERDICT_RU[key] || value.replaceAll('_', ' ')
}

function assessmentLabel(value: string, labels: Record<string, string>): string {
  const key = value.trim().toLowerCase()
  return labels[key] || value || 'нет данных'
}

function formatMoney(value: unknown): string {
  const raw = asString(value).trim()
  if (!raw || raw === '—') return raw || '—'

  const hasRuble = /(?:RUB|руб(?:\.|лей|ля|ль)?|₽)/i.test(raw)
  const numeric = Number(
    raw
      .replace(/(?:RUB|руб(?:\.|лей|ля|ль)?|₽)/gi, '')
      .replace(/[\s\u00a0]/g, '')
      .replace(',', '.'),
  )
  if (!Number.isFinite(numeric)) return raw

  const fractionDigits = Number.isInteger(numeric) ? 0 : 2
  const formatted = new Intl.NumberFormat('ru-RU', {
    minimumFractionDigits: 0,
    maximumFractionDigits: fractionDigits,
  }).format(numeric)
  return hasRuble ? `${formatted} ₽` : formatted
}

function formatMoneyText(value: string): string {
  return value.replace(
    /(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?\s*(?:RUB|руб(?:\.|лей|ля|ль)?|₽)/gi,
    (amount) => formatMoney(amount),
  )
}

function parseManualInput(value: string, fallbackEntityType: ManualEntityType): ManualInput {
  const ids: string[] = []
  const detected: Array<{ entityType: 'lead' | 'deal'; id: string }> = []
  const invalid: string[] = []

  for (const token of value.split(/[\s,;]+/).filter(Boolean)) {
    const link = token.match(/\/crm\/(lead|deal)\/details\/(\d+)(?:[/?#]|$)/i)
    if (link) {
      const entityType = link[1].toLowerCase() as 'lead' | 'deal'
      const id = link[2]
      ids.push(id)
      detected.push({ entityType, id })
      continue
    }
    if (/^\d+$/.test(token)) {
      ids.push(token)
      continue
    }
    invalid.push(token)
  }

  const uniqueIds = [...new Set(ids)]
  const types = [...new Set(detected.map((item) => item.entityType))]
  if (types.length > 1) {
    return {
      ids: uniqueIds,
      entityType: fallbackEntityType,
      detected,
      error: 'За один запуск вставьте ссылки только на лиды или только на сделки.',
    }
  }
  if (invalid.length) {
    return {
      ids: uniqueIds,
      entityType: fallbackEntityType,
      detected,
      error: `Не удалось распознать: ${invalid.slice(0, 2).join(', ')}`,
    }
  }
  return {
    ids: uniqueIds,
    entityType: types[0] || fallbackEntityType,
    detected,
    error: null,
  }
}

function toast(message: string, setter: (value: string | null) => void) {
  setter(message)
  window.setTimeout(() => setter(null), 2200)
}

type ManagerBrief = {
  task: string
  goal: string
  clientText: string
  clientChannel: string
  crm: string
}

function getManagerBrief(analysis: Record<string, unknown> | null | undefined): ManagerBrief {
  const rop = asRecord(analysis?.rop_manager_message_block)
  const manager = asRecord(analysis?.manager_action_block)
  const primary = asRecord(manager.primary_text)
  return {
    task: formatMoneyText(asString(rop.message_to_manager) || asString(rop.check_for_rop) || '—'),
    goal: formatMoneyText(asString(rop.success_condition) || asString(rop.expected_crm_update) || '—'),
    clientText: formatMoneyText(
    asString(primary.email_or_messenger) ||
    asString(primary.call_script) ||
    asString(primary.text) ||
    'Текст клиенту не сформирован.',
    ),
    clientChannel: asString(manager.recommended_channel),
    crm: formatMoneyText(
      asStringList(manager.manager_checklist).join('\n') || asString(rop.expected_crm_update) || '—',
    ),
  }
}

function buildManagerCopy(brief: ManagerBrief): string {
  const channel = brief.clientChannel ? ` (${brief.clientChannel})` : ''
  return `Задача:
${brief.task}

Цель:
${brief.goal}

Текст клиенту${channel}:
${brief.clientText}

Что зафиксировать в CRM:
${brief.crm}`
}

function evidenceList(analysis: Record<string, unknown> | null | undefined): string[] {
  const rop = asRecord(analysis?.rop_manager_message_block)
  const money = asRecord(analysis?.money_path_diagnosis)
  const loss = asRecord(analysis?.loss_diagnosis)
  const fromRop = asStringList(rop.evidence)
  const fromMoney = asStringList(money.evidence)
  const fromLoss = asStringList(loss.evidence)
  return [...fromRop, ...fromMoney, ...fromLoss].slice(0, 8).map(formatMoneyText)
}

function unknownsList(analysis: Record<string, unknown> | null | undefined): string[] {
  const price = asRecord(analysis?.price_comparability_check)
  const payment = asRecord(analysis?.payment_blocker)
  const items = [
    ...asStringList(price.what_is_unclear),
    ...asStringList(payment.missing_confirmation),
  ]
  if (!items.length) {
    const risk = asRecord(analysis?.main_risk)
    if (risk.description) items.push(asString(risk.description))
  }
  return items.slice(0, 8).map(formatMoneyText)
}

function closureReasonsList(analysis: Record<string, unknown> | null | undefined): string[] {
  const closed = asRecord(analysis?.closed_deal_review)
  return asStringList(closed.why_closed_questionable).slice(0, 6).map(formatMoneyText)
}

function internalChecksList(analysis: Record<string, unknown> | null | undefined): string[] {
  const resources = asRecord(analysis?.resource_control)
  return asStringList(resources.allowed_work).slice(0, 4).map(formatMoneyText)
}

function ropRecommendations(analysis: Record<string, unknown> | null | undefined): string[] {
  const rop = asRecord(analysis?.rop_manager_message_block)
  const closed = asRecord(analysis?.closed_deal_review)
  const mode = asRecord(analysis?.deal_mode)
  const items = [
    asString(rop.why_it_matters),
    asString(closed.recommended_pipeline_action),
    asString(mode.rop_focus),
  ].filter(Boolean)
  return items.slice(0, 6).map(formatMoneyText)
}

export default function App() {
  const [tab, setTab] = useState<Tab>('summary')
  const [toastMessage, setToastMessage] = useState<string | null>(null)

  const [analysisProfiles, setAnalysisProfiles] = useState<AnalysisProfile[]>([])
  const [activeProfile, setActiveProfile] = useState<AnalysisProfile | null>(null)
  const [dailyPreview, setDailyPreview] = useState<DailyPreview | null>(null)
  const [dailySelected, setDailySelected] = useState<Set<string>>(new Set())
  const [dailyRun, setDailyRun] = useState<DailySummaryRun | null>(null)
  const [dailyLoading, setDailyLoading] = useState(false)
  const [dailyError, setDailyError] = useState<string | null>(null)
  const [customPeriodFrom, setCustomPeriodFrom] = useState(() => dateInTimeZone('Europe/Moscow'))
  const [customPeriodTo, setCustomPeriodTo] = useState(() => dateInTimeZone('Europe/Moscow'))
  const dailySelectedCandidates = useMemo(
    () => (dailyPreview?.candidates || []).filter((candidate) => dailySelected.has(candidate.journey_key || `${candidate.entity_type}:${candidate.entity_id}`)),
    [dailyPreview, dailySelected],
  )
  const dailySelectedLlmCount = useMemo(
    () => dailySelectedCandidates.filter(candidateNeedsLlm).length,
    [dailySelectedCandidates],
  )
  const dailyPaidLimit = Number(dailyPreview?.cost_preview.paid_entity_limit || 0)
  const dailyWillLaunchPaid = Math.min(dailySelectedLlmCount, dailyPaidLimit)
  const dailyEstimatedCost = Math.round(Number(dailyPreview?.cost_preview.estimated_cost_rub_per_entity || 0) * dailyWillLaunchPaid * 100) / 100
  const dailyProgressByKey = useMemo(() => {
    const rows = new Map<string, Partial<EntityProgress>>()
    for (const item of dailyRun?.items || []) {
      if (item.progress) {
        rows.set(`${item.entity_type}:${item.entity_id}`, item.progress)
        rows.set(item.journey_key, item.progress)
      }
    }
    for (const jobState of dailyRun?.job_states || []) {
      for (const progress of Object.values(jobState.entity_progress || {})) {
        rows.set(`${progress.entity_type}:${progress.entity_id}`, progress)
      }
    }
    return rows
  }, [dailyRun])
  const dailyResultByKey = useMemo(() => new Map(
    (dailyRun?.results || []).map((result) => [`${result.entity_type}:${result.entity_id}`, result]),
  ), [dailyRun])
  const dailyProgressStats = useMemo(() => {
    const selectedItems = (dailyRun?.items || []).filter((item) => Boolean(item.selected))
    return {
      done: selectedItems.filter((item) => item.processing_status === 'done').length,
      running: selectedItems.filter((item) => ['queued', 'running'].includes(item.processing_status)).length,
      errors: selectedItems.filter((item) => item.processing_status === 'error').length,
      skipped: selectedItems.filter((item) => item.processing_status === 'skipped_limit').length,
      total: selectedItems.length || dailyRun?.selected_count || 0,
    }
  }, [dailyRun])
  const dailyIsAnalyzing = dailyRun?.status === 'analyzing'
  const dailyIsDone = dailyRun?.status === 'done'
  const dailyHasErrors = dailyRun?.status === 'completed_with_errors' || dailyRun?.status === 'error'

  const [createdDays, setCreatedDays] = useState(15)
  const [modifiedDays, setModifiedDays] = useState(15)
  const [entityFilter, setEntityFilter] = useState<'lead' | 'deal'>('lead')
  const [priorityFilter, setPriorityFilter] = useState<string>('')
  const [reviewView, setReviewView] = useState<CandidateReviewView>('active')
  const [leadCategories, setLeadCategories] = useState<string[]>([])
  const [bantFilter, setBantFilter] = useState<CandidateFilter['bant_filter']>('')
  const [pipelineIds, setPipelineIds] = useState<string[]>([])
  const [stageIds, setStageIds] = useState<string[]>([])
  const [dealPipelines, setDealPipelines] = useState<CrmPipeline[]>([])
  const [leadPipeline, setLeadPipeline] = useState<CrmPipeline | null>(null)
  const [candidatesData, setCandidatesData] = useState<CandidatesResponse | null>(null)
  const [candidatesLoading, setCandidatesLoading] = useState(false)
  const [candidatesError, setCandidatesError] = useState<string | null>(null)
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null)
  const [filtersReady, setFiltersReady] = useState(false)
  const [showCandidateFilters, setShowCandidateFilters] = useState(false)
  const [showPipelinePicker, setShowPipelinePicker] = useState(false)
  const [showStagePicker, setShowStagePicker] = useState(false)

  const [manualIds, setManualIds] = useState('')
  const [showManualAdvanced, setShowManualAdvanced] = useState(false)
  const [options, setOptions] = useState<AnalyzeOptions>({
    entity_type: 'auto',
    ids: '',
    history_days: 60,
    include_related: true,
    include_internal: true,
    download_audio: true,
    redownload_audio: false,
    transcribe_audio: true,
    analyze: true,
    force_llm: false,
    transcript_mode: 'all',
  })
  const [job, setJob] = useState<JobState | null>(null)
  const [jobError, setJobError] = useState<string | null>(null)
  const [selectedResultIndex, setSelectedResultIndex] = useState(0)
  const [pendingAnalyzeMeta, setPendingAnalyzeMeta] = useState<{
    entity_type: AnalyzeOptions['entity_type']
    entity_id: string
  } | null>(null)

  const [history, setHistory] = useState<UiReportListItem[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [selectedReport, setSelectedReport] = useState<UiReportDetail | null>(null)
  const [leadWorkspaceOpen, setLeadWorkspaceOpen] = useState(false)
  const closeLeadWorkspace = useCallback(() => setLeadWorkspaceOpen(false), [])
  const [markdown, setMarkdown] = useState<string | null>(null)
  const [showMarkdown, setShowMarkdown] = useState(false)

  const activeAnalysis = useMemo(() => {
    if (tab === 'history' && selectedReport?.report_json) {
      return unwrapAnalysis(selectedReport.report_json)
    }
    if (job?.results?.[selectedResultIndex]?.analysis) {
      return unwrapAnalysis(job.results[selectedResultIndex].analysis)
    }
    return null
  }, [tab, selectedReport, job, selectedResultIndex])

  const activeMeta = useMemo(() => {
    if (tab === 'history' && selectedReport) {
      return {
        entity_type: selectedReport.entity_type,
        entity_id: selectedReport.entity_id,
        report_id: selectedReport.id,
        risk_level: selectedReport.risk_level,
        attention_reason: selectedReport.attention_reason,
        recommended_action: selectedReport.recommended_action,
        bitrix_url: selectedReport.bitrix_url || null,
        candidate_review: selectedReport.candidate_review || null,
      }
    }
    const result = job?.results?.[selectedResultIndex]
    if (result) {
      return {
        entity_type: result.entity_type,
        entity_id: result.entity_id,
        report_id: result.report_id ?? null,
        risk_level: result.risk_level,
        attention_reason: result.attention_reason,
        recommended_action: result.recommended_action,
        bitrix_url: result.bitrix_url || null,
        candidate_review: null,
      }
    }
    if (tab === 'manual' && job && pendingAnalyzeMeta) {
      return {
        entity_type: pendingAnalyzeMeta.entity_type,
        entity_id: pendingAnalyzeMeta.entity_id,
        report_id: null as number | null,
        risk_level: null,
        attention_reason: 'Анализ выполняется: собираем CRM, аудио, транскрипты и LLM-вывод.',
        recommended_action: 'Дождитесь завершения job — после этого здесь появится поручение менеджеру.',
        bitrix_url: null,
        candidate_review: null,
      }
    }
    if (selectedCandidate && !activeAnalysis) {
      const hasOldAnalysis = Boolean(selectedCandidate.analyzed)
      return {
        entity_type: selectedCandidate.entity_type,
        entity_id: selectedCandidate.entity_id,
        report_id: null as number | null,
        risk_level: selectedCandidate.priority,
        attention_reason: selectedCandidate.attention_reason,
        recommended_action: hasOldAnalysis
          ? 'Откройте прошлый отчёт для контекста или обновите анализ, если данные в CRM изменились'
          : 'Запустить анализ и получить поручение менеджеру',
        bitrix_url: selectedCandidate.bitrix_url || null,
        candidate_review: null,
      }
    }
    return null
  }, [tab, selectedReport, selectedCandidate, activeAnalysis, job, selectedResultIndex, pendingAnalyzeMeta])

  function applyFilterState(filter: CandidateFilter) {
    setEntityFilter(filter.entity_type === 'deal' ? 'deal' : 'lead')
    setCreatedDays(Number(filter.created_days) || 15)
    setModifiedDays(Number(filter.modified_days) || 15)
    setPriorityFilter(filter.priority || '')
    setReviewView(filter.review_view || 'active')
    setLeadCategories(Array.isArray(filter.lead_categories) ? filter.lead_categories.map(String) : [])
    setBantFilter(filter.bant_filter || '')
    setPipelineIds(Array.isArray(filter.pipeline_ids) ? filter.pipeline_ids.map(String) : [])
    setStageIds(Array.isArray(filter.stage_ids) ? filter.stage_ids.map(String) : [])
  }

  function availableStages(): { id: string; name: string }[] {
    if (entityFilter === 'lead') {
      return leadPipeline?.stages || []
    }
    const selected = new Set(pipelineIds)
    const stages: { id: string; name: string }[] = []
    const seen = new Set<string>()
    for (const pipeline of dealPipelines) {
      if (!selected.has(pipeline.id)) continue
      for (const stage of pipeline.stages || []) {
        if (!stage.id || seen.has(stage.id)) continue
        seen.add(stage.id)
        stages.push(stage)
      }
    }
    return stages
  }

  function toggleId(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((item) => item !== id) : [...list, id]
  }

  function selectedPipelineNames(): string[] {
    const selected = new Set(pipelineIds)
    return dealPipelines.filter((pipeline) => selected.has(pipeline.id)).map((pipeline) => pipeline.name)
  }

  function selectedStageNames(): string[] {
    const selected = new Set(stageIds)
    return availableStages().filter((stage) => selected.has(stage.id)).map((stage) => stage.name)
  }

  function shortListSummary(items: string[], empty: string): string {
    if (!items.length) return empty
    const preview = items.slice(0, 2).join(', ')
    return items.length > 2 ? `${preview} +${items.length - 2}` : preview
  }

  function reportPreview(item: UiReportListItem): string {
    const action = item.recommended_action || item.attention_reason || 'без краткого вывода'
    const date = item.created_at ? item.created_at.slice(0, 16).replace('T', ' ') : ''
    const risk = item.risk_level ? `${riskLabelRu(item.risk_level)} риск` : 'риск не указан'
    const firstSentence = action.split(/[.!?]/, 1)[0].trim()
    const shortAction = firstSentence.length > 96 ? `${firstSentence.slice(0, 93).trim()}...` : firstSentence
    return `${date} · ${risk} · ${shortAction}`
  }

  function reviewViewLabel(value: CandidateReviewView): string {
    if (value === 'reviewed') return 'Проверенные РОПом'
    if (value === 'all') return 'Все'
    return 'На проверку'
  }

  async function loadCandidates(overrides?: {
    entity_type?: 'lead' | 'deal'
    created_days?: number
    modified_days?: number
    priority?: string
    review_view?: CandidateReviewView
    pipeline_ids?: string[]
    stage_ids?: string[]
    lead_categories?: string[]
    bant_filter?: CandidateFilter['bant_filter']
  }) {
    setCandidatesLoading(true)
    setCandidatesError(null)
    try {
      const data = await searchCandidates({
        entity_type: overrides?.entity_type ?? entityFilter,
        created_days: overrides?.created_days ?? createdDays,
        modified_days: overrides?.modified_days ?? modifiedDays,
        limit: 20,
        priority: (overrides?.priority ?? priorityFilter) || null,
        review_view: overrides?.review_view ?? reviewView,
        pipeline_ids: overrides?.pipeline_ids ?? pipelineIds,
        stage_ids: overrides?.stage_ids ?? stageIds,
        lead_categories: overrides?.lead_categories ?? leadCategories,
        bant_filter: overrides?.bant_filter ?? bantFilter,
        save: true,
      })
      setCandidatesData(data)
      if (data.candidates.length) {
        setSelectedCandidate((prev) => {
          if (!prev) return data.candidates[0]
          const still = data.candidates.find(
            (item) => item.entity_type === prev.entity_type && item.entity_id === prev.entity_id,
          )
          return still || data.candidates[0]
        })
      } else {
        setSelectedCandidate(null)
      }
    } catch (error) {
      setCandidatesError(error instanceof Error ? error.message : String(error))
    } finally {
      setCandidatesLoading(false)
    }
  }

  async function loadHistory(selectLatest = false) {
    setHistoryError(null)
    try {
      const data = await fetchReports(50)
      setHistory(data.items)
      if (selectLatest && data.items[0]) {
        setShowMarkdown(false)
        setMarkdown(null)
        const detail = await fetchReport(data.items[0].id, false)
        setSelectedReport(detail)
        setLeadWorkspaceOpen(detail.entity_type === 'lead')
      }
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : String(error))
    }
  }

  // Первая загрузка: справочник воронок + сохранённый фильтр из БД.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      setCandidatesLoading(true)
      setCandidatesError(null)
      setHistoryError(null)
      try {
        const [pipelines, savedFilter, reports, profiles] = await Promise.all([
          fetchPipelines(),
          fetchCandidateFilter(),
          fetchReports(50),
          fetchAnalysisProfiles(),
        ])
        if (cancelled) return
        setDealPipelines(pipelines.deal_pipelines || [])
        setLeadPipeline(pipelines.lead_pipeline || null)
        setHistory(reports.items)
        setAnalysisProfiles(profiles.items)
        setActiveProfile(profiles.selected)

        const filter = savedFilter.filter
        applyFilterState(filter)
        setFiltersReady(true)

        // Live Bitrix discovery остаётся ручным: не запускаем его только из-за открытия UI.
        setCandidatesData(null)
        setSelectedCandidate(null)
      } catch (error) {
        if (cancelled) return
        const message = error instanceof Error ? error.message : String(error)
        setCandidatesError(message)
        setHistoryError(message)
        setFiltersReady(true)
      } finally {
        if (!cancelled) setCandidatesLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const jobId = job?.job_id
  const jobStatus = job?.status
  useEffect(() => {
    if (!jobId || jobStatus === 'done' || jobStatus === 'error') return
    const timer = window.setInterval(() => {
      void fetchJob(jobId)
        .then((next) => {
          setJob(next)
          if (next.status === 'done') {
            void fetchReports(50)
              .then((data) => {
                setHistory(data.items)
                const result = next.results?.[0]
                const reportId = result?.report_id || next.report_ids?.[0]
                if (reportId) void openHistoryReport(Number(reportId))
              })
              .catch((error) => setHistoryError(error instanceof Error ? error.message : String(error)))
            toast('Анализ завершён', setToastMessage)
          }
        })
        .catch((error) => setJobError(error instanceof Error ? error.message : String(error)))
    }, 2000)
    return () => window.clearInterval(timer)
  }, [jobId, jobStatus])

  async function runAnalyze(ids: string, entityType: AnalyzeOptions['entity_type'] = options.entity_type) {
    setJobError(null)
    setSelectedResultIndex(0)
    setShowMarkdown(false)
    setMarkdown(null)
    const firstId = ids.split(/[\s,;]+/).find(Boolean) || ids.trim()
    setPendingAnalyzeMeta({ entity_type: entityType, entity_id: firstId || '—' })
    try {
      const confirmPaid = options.force_llm
        ? window.confirm('Ручной ID может быть вне активного профиля. Подтвердить принудительный платный LLM-анализ?')
        : true
      if (!confirmPaid) return
      const started = await startAnalyze({ ...options, ids, entity_type: entityType, confirm_paid: options.force_llm })
      setJob(started)
      setTab('manual')
      toast('Анализ запущен', setToastMessage)
    } catch (error) {
      setJobError(error instanceof Error ? error.message : String(error))
    }
  }

  async function openHistoryReport(reportId: number) {
    setShowMarkdown(false)
    setMarkdown(null)
    const detail = await fetchReport(reportId, false)
    setSelectedReport(detail)
    setLeadWorkspaceOpen(detail.entity_type === 'lead')
    setTab('history')
  }

  async function openCandidateReport(candidate: Candidate) {
    let report = history.find(
      (item) => item.entity_type === candidate.entity_type && String(item.entity_id) === String(candidate.entity_id),
    )
    if (!report) {
      const data = await fetchReports(50)
      setHistory(data.items)
      report = data.items.find(
        (item) => item.entity_type === candidate.entity_type && String(item.entity_id) === String(candidate.entity_id),
      )
    }
    if (!report) {
      toast('Сохранённый отчёт пока не найден', setToastMessage)
      return
    }
    await openHistoryReport(report.id)
  }

  async function toggleMarkdown() {
    if (!activeMeta?.report_id) return
    if (showMarkdown) {
      setShowMarkdown(false)
      return
    }
    const data = await fetchReportMarkdown(activeMeta.report_id)
    setMarkdown(data.markdown)
    setShowMarkdown(true)
  }

  async function onDecision(decision: string) {
    if (!activeMeta?.report_id) {
      toast('Сначала нужен сохранённый отчёт анализа', setToastMessage)
      return
    }
    await saveDecision(activeMeta.report_id, decision)
    if (selectedReport?.id === activeMeta.report_id) {
      const detail = await fetchReport(activeMeta.report_id, false)
      setSelectedReport(detail)
    }
    toast('Решение РОПа сохранено', setToastMessage)
  }

  async function onOutcome(outcome: string) {
    if (!activeMeta?.report_id) {
      toast('Сначала нужен сохранённый отчёт анализа', setToastMessage)
      return
    }
    await saveOutcome(activeMeta.report_id, outcome)
    if (selectedReport?.id === activeMeta.report_id) {
      const detail = await fetchReport(activeMeta.report_id, false)
      setSelectedReport(detail)
    }
    toast('Исход сохранён', setToastMessage)
  }

  async function onQualificationReview(payload: {
    is_correct: boolean
    issue_fields?: string[]
    corrected_statuses?: Record<string, string>
    corrected_category?: string | null
    comment?: string | null
  }) {
    if (!activeMeta?.report_id) {
      toast('Сначала нужен сохранённый отчёт анализа', setToastMessage)
      return
    }
    await saveQualificationReview(activeMeta.report_id, payload)
    if (selectedReport?.id === activeMeta.report_id) {
      const detail = await fetchReport(activeMeta.report_id, false)
      setSelectedReport(detail)
    }
    toast('Проверка квалификации сохранена', setToastMessage)
  }

  function patchActiveProfile(patch: Partial<AnalysisProfile['profile']>) {
    setActiveProfile((current) => current ? { ...current, profile: { ...current.profile, ...patch } } : current)
    setDailyPreview(null)
    setDailyRun(null)
  }

  async function saveActiveProfile() {
    if (!activeProfile) return
    setDailyError(null)
    try {
      const result = await updateAnalysisProfile(activeProfile)
      setActiveProfile(result.profile)
      setAnalysisProfiles((items) => items.map((item) => item.id === result.profile.id ? result.profile : item))
      setDailyPreview(null)
      toast('Профиль сохранён', setToastMessage)
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    }
  }

  async function saveProfileAs() {
    if (!activeProfile) return
    const name = window.prompt('Название нового профиля', `${activeProfile.name} — копия`)?.trim()
    if (!name) return
    setDailyError(null)
    try {
      const result = await createAnalysisProfile(name, activeProfile.profile)
      setAnalysisProfiles((items) => [...items, result.profile])
      setActiveProfile(result.profile)
      setDailyPreview(null)
      toast('Новый профиль создан', setToastMessage)
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    }
  }

  async function removeActiveProfile() {
    if (!activeProfile || !window.confirm(`Удалить профиль «${activeProfile.name}»?`)) return
    setDailyError(null)
    try {
      const result = await deleteAnalysisProfile(activeProfile.id)
      setAnalysisProfiles(result.items)
      setActiveProfile(result.selected)
      setDailyPreview(null)
      toast('Профиль удалён', setToastMessage)
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    }
  }

  async function chooseProfile(profileId: number) {
    const local = analysisProfiles.find((item) => item.id === profileId)
    if (local) setActiveProfile(local)
    setDailyPreview(null)
    setDailyRun(null)
    try {
      const result = await selectAnalysisProfile(profileId)
      setActiveProfile(result.selected)
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    }
  }

  async function loadDailyPreview() {
    if (!activeProfile) return
    const periodPreset = activeProfile.profile.period_preset
    if (periodPreset === 'custom' && (!customPeriodFrom || !customPeriodTo || customPeriodFrom > customPeriodTo)) {
      setDailyError('Укажите корректный произвольный период: дата начала не позже даты окончания')
      return
    }
    setDailyLoading(true)
    setDailyError(null)
    setDailyRun(null)
    try {
      const data = await previewAnalysisProfile(activeProfile.id, {
        period_preset: periodPreset,
        ...(periodPreset === 'custom' ? { date_from: customPeriodFrom, date_to: customPeriodTo } : {}),
      })
      setDailyPreview(data)
      setDailySelected(new Set(data.candidates.filter((item) => item.workset_selected).map((item) => item.journey_key || `${item.entity_type}:${item.entity_id}`)))
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    } finally {
      setDailyLoading(false)
    }
  }

  function clearDailySelection() {
    if (dailyRun) return
    setDailySelected(new Set())
  }

  function selectDailyToLimit() {
    if (!dailyPreview || dailyRun) return
    const limit = Math.max(0, Number(dailyPreview.cost_preview.paid_entity_limit || 0))
    const keys = dailyPreview.candidates
      .filter(candidateNeedsLlm)
      .slice(0, limit)
      .map((candidate) => candidate.journey_key || `${candidate.entity_type}:${candidate.entity_id}`)
    setDailySelected(new Set(keys))
  }

  async function freezeDailySummary() {
    if (!activeProfile || !dailyPreview || !dailySelected.size) return
    setDailyLoading(true)
    setDailyError(null)
    try {
      const run = await createDailySummary(activeProfile, dailyPreview, [...dailySelected])
      setDailyRun(run)
      toast('Snapshot сводки сохранён', setToastMessage)
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    } finally {
      setDailyLoading(false)
    }
  }

  async function launchDailySummary() {
    if (!dailyRun) return
    const paid = dailyRun.llm_allowed_count > 0
    if (paid && !window.confirm(`Подтвердить платный анализ максимум ${dailyRun.llm_allowed_count} карточек? Остальные останутся в резерве.`)) return
    setDailyLoading(true)
    setDailyError(null)
    try {
      const result = await startDailySummary(dailyRun.id, paid)
      setDailyRun(result.summary)
      const reusedSuffix = result.reused_count ? `, готовых отчётов взято: ${result.reused_count}` : ''
      toast(`Обработано карточек: ${result.started_count}${reusedSuffix}`, setToastMessage)
    } catch (error) {
      setDailyError(error instanceof Error ? error.message : String(error))
    } finally {
      setDailyLoading(false)
    }
  }

  const dailyRunId = dailyRun?.id
  const dailyRunStatus = dailyRun?.status
  useEffect(() => {
    if (!dailyRunId || dailyRunStatus !== 'analyzing') return
    const timer = window.setInterval(() => {
      void fetchDailySummary(dailyRunId)
        .then((value) => setDailyRun(value))
        .catch((error) => setDailyError(error instanceof Error ? error.message : String(error)))
    }, 2000)
    return () => window.clearInterval(timer)
  }, [dailyRunId, dailyRunStatus])

  const summary = candidatesData?.summary
  const manualInput = parseManualInput(manualIds, options.entity_type)
  const managerBrief = getManagerBrief(activeAnalysis)
  const copyText = buildManagerCopy(managerBrief)
  const facts = activeAnalysis ? evidenceList(activeAnalysis) : selectedCandidate?.reasons || []
  const unknowns = activeAnalysis ? unknownsList(activeAnalysis) : ['Полный разбор появится после LLM-анализа']
  const closureReasons = activeAnalysis ? closureReasonsList(activeAnalysis) : []
  const internalChecks = activeAnalysis ? internalChecksList(activeAnalysis) : []
  const recommendations = activeAnalysis
    ? ropRecommendations(activeAnalysis)
    : selectedCandidate
      ? ['Выберите кандидата и запустите анализ', selectedCandidate.attention_reason]
      : []

  return (
    <div className="wrap">
      <div className="top">
        <div className="brand">
          <div className="logo">Р</div>
          Помощник РОПа Практик-М
        </div>
        <div className="nav">
          <button className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>
            Ежедневная сводка
          </button>
          <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>
            Кандидаты
          </button>
          <button className={tab === 'manual' ? 'active' : ''} onClick={() => setTab('manual')}>
            Ручной запуск
          </button>
          <button className={tab === 'history' ? 'active' : ''} onClick={() => { setTab('history'); void loadHistory(true) }}>
            История
          </button>
        </div>
        <div className="pill">локально · read-only Bitrix</div>
      </div>

      <section className="hero">
        <div className="hero-copy">
          <div className="hero-label">Контроль пути от лида до денег</div>
          <h1>Что РОПу проверить сегодня</h1>
          <p>Риск в сделке, действие и готовое поручение менеджеру.</p>
        </div>
      </section>

      {tab === 'summary' && (
        <div className="daily-layout">
          <aside className="panel daily-profile-panel">
            <div className="panel-head">
              <div><h3>Профиль анализа</h3><p>Последний выбранный профиль загрузится автоматически.</p></div>
            </div>
            <div className="field">
              <label>Профиль</label>
              <select value={activeProfile?.id || ''} onChange={(event) => void chooseProfile(Number(event.target.value))}>
                {analysisProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
              </select>
            </div>
            {activeProfile ? <>
              <div className="field">
                <label>Название</label>
                <input value={activeProfile.name} onChange={(event) => setActiveProfile({ ...activeProfile, name: event.target.value })} />
              </div>
              <div className="field">
                <label>Период по Москве</label>
                <select value={activeProfile.profile.period_preset} onChange={(event) => patchActiveProfile({ period_preset: event.target.value as AnalysisProfile['profile']['period_preset'] })}>
                  <option value="today_and_previous_workday">Сегодня + предыдущий рабочий день</option>
                  <option value="today">Только сегодня</option>
                  <option value="previous_workday">Предыдущий рабочий день</option>
                  <option value="custom">Произвольный период</option>
                </select>
              </div>
              {activeProfile.profile.period_preset === 'custom' ? <div className="daily-custom-period">
                <div className="field">
                  <label>Дата с</label>
                  <input type="date" value={customPeriodFrom} onChange={(event) => { setCustomPeriodFrom(event.target.value); setDailyPreview(null); setDailyRun(null) }} />
                </div>
                <div className="field">
                  <label>Дата по</label>
                  <input type="date" value={customPeriodTo} onChange={(event) => { setCustomPeriodTo(event.target.value); setDailyPreview(null); setDailyRun(null) }} />
                </div>
                <p className="input-hint">Обе даты включаются в сводку и применяются только к текущему запуску.</p>
              </div> : null}
              <div className="daily-limits">
                {([
                  ['workset', 'Выделить карточек'],
                  ['new_slots', 'Новых'],
                  ['backlog_slots', 'Backlog'],
                  ['paid_per_run', 'Платных / запуск'],
                  ['paid_per_day', 'Платных / день'],
                ] as const).map(([key, label]) => <div className="field" key={key}>
                  <label>{label}</label>
                  <input type="number" min={0} max={100} value={activeProfile.profile.limits[key]} onChange={(event) => patchActiveProfile({ limits: { ...activeProfile.profile.limits, [key]: Math.max(0, Number(event.target.value) || 0) } })} />
                </div>)}
              </div>
              <details className="profile-stages">
                <summary>Этапы лидов</summary>
                <label><input type="checkbox" checked={Boolean(activeProfile.profile.lead.all_stages)} onChange={(event) => patchActiveProfile({ lead: { ...activeProfile.profile.lead, all_stages: event.target.checked } })} /> Все этапы, кроме исключённых категорий</label>
                {!activeProfile.profile.lead.all_stages ? <div className="profile-stage-list">{(leadPipeline?.stages || []).map((stage) => {
                  const selectedStages = ((activeProfile.profile.lead.stage_ids as string[]) || []).map(String)
                  return <label key={stage.id}><input type="checkbox" checked={selectedStages.includes(stage.id)} onChange={() => patchActiveProfile({ lead: { ...activeProfile.profile.lead, stage_ids: toggleId(selectedStages, stage.id) } })} /> {stage.name}</label>
                })}</div> : null}
              </details>
              <details className="profile-stages">
                <summary>Воронки и этапы сделок</summary>
                <p className="muted">Старые активные сделки выбранной воронки остаются в портфеле независимо от даты создания.</p>
                {dealPipelines.map((pipeline) => {
                  const selectedPipelines = ((activeProfile.profile.deal.pipeline_ids as string[]) || []).map(String)
                  const checked = selectedPipelines.includes(pipeline.id)
                  return <div className="profile-pipeline" key={pipeline.id}>
                    <label><input type="checkbox" checked={checked} onChange={() => patchActiveProfile({ deal: { ...activeProfile.profile.deal, pipeline_ids: toggleId(selectedPipelines, pipeline.id) } })} /> {pipeline.name}</label>
                    {checked ? <div className="profile-stage-list">{pipeline.stages.map((stage) => {
                      const selectedStages = ((activeProfile.profile.deal.stage_ids as string[]) || []).map(String)
                      return <label key={stage.id}><input type="checkbox" checked={selectedStages.includes(stage.id)} onChange={() => patchActiveProfile({ deal: { ...activeProfile.profile.deal, stage_ids: toggleId(selectedStages, stage.id) } })} /> {stage.name}</label>
                    })}</div> : null}
                  </div>
                })}
              </details>
              <div className="profile-actions">
                <button className="btn secondary" onClick={() => void saveActiveProfile()}>Сохранить</button>
                <button className="btn ghost" onClick={() => void saveProfileAs()}>Сохранить как</button>
                <button className="btn ghost" onClick={() => void removeActiveProfile()}>Удалить</button>
              </div>
              <button className="btn" disabled={dailyLoading} onClick={() => void loadDailyPreview()}>{dailyLoading ? 'Собираем live-данные…' : 'Обновить кандидатов без LLM'}</button>
            </> : <p className="muted">Профиль загружается…</p>}
          </aside>

          <main>
            <section className="section daily-head">
              <div>
                <div className="hero-label">Ручной MVP · Bitrix read-only</div>
                <h2>Кандидаты ежедневной сводки</h2>
                <p className="muted">Весь список виден. Синим выделен рабочий набор; резерв можно добавить вручную.</p>
              </div>
              {dailyPreview ? <div className="daily-stats">
                <span>Всего <b>{dailyPreview.summary.total ?? 0}</b></span>
                <span>Выбрано <b>{dailySelected.size}</b></span>
                <span>Нужен LLM всего <b>{dailyPreview.summary.llm_required ?? 0}</b></span>
                <span>В выбранных <b>{dailySelectedLlmCount}</b></span>
                <span>Будет платных <b>{dailyWillLaunchPaid}</b></span>
                <span>Доступно сегодня <b>{asString(dailyPreview.cost_preview.paid_entity_limit, '0')}</b></span>
              </div> : null}
            </section>
            {dailyError ? <div className="alert error"><strong>Ошибка:</strong> {dailyError}</div> : null}
            {dailyPreview && asString(asRecord(dailyPreview.scope.handoff_warning).message) ? <div className="alert">
              {asString(asRecord(dailyPreview.scope.handoff_warning).message)} Количество: {asString(asRecord(dailyPreview.scope.handoff_warning).outside_profile_count, '0')}.
            </div> : null}
            {dailyPreview && asString(asRecord(dailyPreview.scope.profile_drift).message) ? <div className="alert error">
              {asString(asRecord(dailyPreview.scope.profile_drift).message)}
            </div> : null}
            {dailyPreview ? <>
              <div className="daily-selection-actions">
                <button className="btn ghost" type="button" disabled={Boolean(dailyRun) || !dailySelected.size} onClick={clearDailySelection}>Снять всё</button>
                <button className="btn secondary" type="button" disabled={Boolean(dailyRun) || dailyPaidLimit <= 0} onClick={selectDailyToLimit}>Выбрать до лимита</button>
                {dailyRun && dailyRun.status !== 'draft' ? <div className={`daily-live-summary ${dailyIsDone ? 'done' : dailyHasErrors ? 'error' : ''}`} role="status" aria-live="polite">
                  <strong className="progress-stage-label">
                    {dailyIsAnalyzing ? <ActivitySpinner /> : dailyIsDone ? <span className="status-check" aria-hidden="true">✓</span> : dailyHasErrors ? <span className="status-error" aria-hidden="true">!</span> : null}
                    {dailyIsAnalyzing ? 'Анализ выполняется' : dailyIsDone ? 'Сводка готова' : dailyHasErrors ? 'Завершено с ошибками' : 'Обработка завершена'}
                  </strong>
                  <span>Готово {dailyProgressStats.done} из {dailyProgressStats.total}</span>
                  <span>В работе: {dailyProgressStats.running}</span>
                  {dailyProgressStats.errors ? <span>Ошибок: {dailyProgressStats.errors}</span> : null}
                  {dailyProgressStats.skipped ? <span>Не запущено: {dailyProgressStats.skipped}</span> : null}
                </div> : null}
              </div>
              <div className="daily-groups">
                {([
                  {
                    key: 'new',
                    title: 'Новые случаи',
                    description: 'Проблемы и риски, впервые попавшие в текущую сводку.',
                    candidates: dailyPreview.candidates.filter((candidate) => (candidate.lifecycle || 'new') === 'new'),
                  },
                  {
                    key: 'backlog',
                    title: 'Накопившиеся и вернувшиеся в контроль',
                    description: 'Неразобранные случаи прошлых запусков и случаи со значимыми изменениями.',
                    candidates: dailyPreview.candidates.filter((candidate) => (candidate.lifecycle || 'new') !== 'new'),
                  },
                ] as const).map((group) => <section className={`daily-group daily-group-${group.key}`} key={group.key}>
                  <div className="daily-group-head">
                    <div><h3>{group.title}</h3><p>{group.description}</p></div>
                    <span>{group.candidates.length}</span>
                  </div>
                  <div className="daily-cards">
                  {group.candidates.map((candidate) => {
                  const key = candidate.journey_key || `${candidate.entity_type}:${candidate.entity_id}`
                  const entityKey = `${candidate.entity_type}:${candidate.entity_id}`
                  const checked = dailySelected.has(key)
                  const runResult = dailyResultByKey.get(entityKey)
                  const progress = dailyProgressByKey.get(entityKey) || dailyProgressByKey.get(key)
                  return <article className={`daily-candidate ${checked ? 'selected' : 'reserve'}`} key={key}>
                    <label className="daily-select">
                      <input type="checkbox" checked={checked} disabled={Boolean(dailyRun)} onChange={() => setDailySelected((current) => {
                        const next = new Set(current)
                        if (next.has(key)) next.delete(key); else next.add(key)
                        return next
                      })} />
                      {checked ? 'В рабочем наборе' : 'Добавить из резерва'}
                    </label>
                    <div className="daily-card-title"><span className={`risk-dot ${candidate.priority}`} /> <b>{candidate.title}</b></div>
                    <div className="candidate-meta">{candidate.entity_type === 'lead' ? 'Лид' : 'Сделка'} {candidate.entity_id} · {candidate.status} · {(candidate.lifecycle || 'new') === 'new' ? 'новый случай' : candidate.lifecycle === 'reactivation' ? 'вернулся в контроль' : 'накопившийся случай'}</div>
                    {runResult ? <>
                      <p>{runResult.attention_reason || (runResult.has_analysis ? 'Анализ готов' : 'Контекст собран без нового анализа')}</p>
                      {runResult.entity_type === 'lead' ? <LeadQualificationStrip summary={runResult.lead_qualification} hasAnalysis={runResult.has_analysis} category={runResult.lead_category} /> : null}
                      {runResult.recommended_action ? <p>{runResult.recommended_action}</p> : null}
                      {runResult.report_id ? <button className="btn ghost" onClick={() => void openHistoryReport(Number(runResult.report_id))}>Открыть отчёт</button> : null}
                    </> : <>
                      <p>{candidate.attention_reason}</p>
                      <div className="reason-codes">{(candidate.reason_codes || []).map((code) => <span key={code}>{code}</span>)}</div>
                      <div className="candidate-meta">Анализ: {candidate.analysis_freshness || 'missing'} · звонки: {asString(candidate.call_method?.attempts, '0')} · входящие: {asString(candidate.call_method?.incoming, '0')} · исходящие: {asString(candidate.call_method?.outgoing, '0')}</div>
                      {progress && checked && dailyRun?.status !== 'draft' ? <EntityProgressView progress={progress} /> : candidate.entity_type === 'lead' ? <LeadQualificationStrip summary={candidate.lead_qualification} hasAnalysis={Boolean(candidate.lead_analysis_available || candidate.analyzed)} category={candidate.lead_category} /> : null}
                    </>}
                    {candidate.bitrix_url ? <a className="bitrix-link" href={candidate.bitrix_url} target="_blank" rel="noreferrer">Открыть в Bitrix</a> : null}
                  </article>
                  })}
                  {!group.candidates.length ? <p className="daily-group-empty">Сейчас случаев в этом разделе нет.</p> : null}
                  </div>
                </section>)}
              </div>
              {!dailyPreview.candidates.length ? <div className="section"><p className="muted">По текущему профилю сигналов не найдено.</p></div> : null}
              <section className="section daily-run-bar">
                <div>
                  <b>Платный gate</b>
                  <p>{asString(dailyPreview.cost_preview.message)} Оценка выбранного запуска: {dailyEstimatedCost} ₽. Зарезервировано сегодня: {asString(dailyPreview.cost_preview.paid_used_today, '0')}.</p>
                  {dailyRun?.actual_cost ? <p>Фактическая стоимость завершённых анализов: {asString(asRecord(dailyRun.actual_cost).estimated_cost_rub, '0')} ₽.</p> : null}
                </div>
                {!dailyRun ? <button className="btn" disabled={!dailySelected.size || dailyLoading} onClick={() => void freezeDailySummary()}>
                  <span className="btn-content">{dailyLoading ? <ActivitySpinner /> : null}{dailyLoading ? 'Фиксируем сводку…' : 'Зафиксировать snapshot'}</span>
                </button> : <button className={`btn daily-run-action ${dailyRun.status}`} disabled={dailyLoading || dailyRun.status !== 'draft'} onClick={() => void launchDailySummary()}>
                  <span className="btn-content">
                    {dailyLoading || dailyIsAnalyzing ? <ActivitySpinner /> : dailyIsDone ? <span className="status-check" aria-hidden="true">✓</span> : dailyHasErrors ? <span className="status-error" aria-hidden="true">!</span> : null}
                    {dailyLoading ? 'Запускаем анализ…' : dailyRun.status === 'draft' ? 'Подтвердить и запустить выбранные' : dailyIsAnalyzing ? `Анализ выполняется · ${dailyProgressStats.done} из ${dailyProgressStats.total}` : dailyIsDone ? 'Сводка готова' : dailyHasErrors ? 'Завершено с ошибками' : `Статус: ${dailyRun.status}`}
                  </span>
                </button>}
              </section>
            </> : <section className="section"><p className="muted">Настройте профиль и нажмите «Обновить кандидатов без LLM». Старые анализы не используются как источник очереди.</p></section>}
          </main>
        </div>
      )}

      {tab === 'dashboard' && (
        <div className="grid">
          <aside className="panel">
            <div className="panel-head">
              <div>
                <h3>Очередь контроля</h3>
                <p>Топ-20 по выбранным этапам.</p>
              </div>
              <span className="queue-count">{summary?.returned ?? '—'}</span>
            </div>
            <div className="queue-actions">
              <button
                className="btn secondary"
                onClick={() => void loadCandidates()}
                disabled={candidatesLoading || !filtersReady}
              >
                {candidatesLoading ? 'Загрузка…' : 'Обновить'}
              </button>
              <button
                className="btn ghost"
                onClick={() => setShowCandidateFilters((value) => !value)}
                type="button"
              >
                {showCandidateFilters ? 'Скрыть' : 'Фильтры'}
              </button>
            </div>
            <div className="filter-summary">
              {entityFilter === 'deal' ? 'Сделки' : 'Лиды'} · создано {createdDays} дн. · изменено {modifiedDays} дн.
              {priorityFilter ? ` · ${riskLabelRu(priorityFilter)}` : ''}
              {entityFilter === 'lead' && leadCategories.length ? ` · категория ${leadCategories.join(', ')}` : ''}
              {entityFilter === 'lead' && bantFilter ? ` · BANT: ${BANT_FILTER_RU[bantFilter] || bantFilter}` : ''}
              {` · ${reviewViewLabel(reviewView)}`}
              {entityFilter === 'deal' ? ` · ${shortListSummary(selectedPipelineNames(), 'воронки не выбраны')}` : ''}
              {` · ${shortListSummary(selectedStageNames(), 'этапы не выбраны')}`}
            </div>
            {showCandidateFilters && (
              <div className="filters filters-compact">
              <div className="field field-days">
                <label>Созданы</label>
                <input
                  type="number"
                  min={0}
                  value={createdDays}
                  onChange={(e) => setCreatedDays(Math.max(0, Number(e.target.value) || 0))}
                />
              </div>
              <div className="field field-days">
                <label>Изменены</label>
                <input
                  type="number"
                  min={0}
                  value={modifiedDays}
                  onChange={(e) => setModifiedDays(Math.max(0, Number(e.target.value) || 0))}
                />
              </div>
              <div className="field">
                <label>Тип</label>
                <select
                  value={entityFilter}
                  onChange={(e) => {
                    const next = e.target.value as 'lead' | 'deal'
                    setEntityFilter(next)
                    // При смене типа сбрасываем воронки/этапы — иначе можно искать «не то».
                    setPipelineIds([])
                    setStageIds([])
                    setLeadCategories([])
                    setBantFilter('')
                    setShowPipelinePicker(false)
                    setShowStagePicker(false)
                    setCandidatesData(null)
                    setSelectedCandidate(null)
                  }}
                >
                  <option value="lead">Лиды</option>
                  <option value="deal">Сделки</option>
                </select>
              </div>
              <div className="field">
                <label>Приоритет</label>
                <select value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)}>
                  <option value="">Любой</option>
                  <option value="high">high</option>
                  <option value="medium">medium</option>
                  <option value="low">low</option>
                </select>
              </div>
              <div className="field">
                <label>Очередь</label>
                <select value={reviewView} onChange={(e) => setReviewView(e.target.value as CandidateReviewView)}>
                  <option value="active">На проверку</option>
                  <option value="reviewed">Проверенные РОПом</option>
                  <option value="all">Все</option>
                </select>
              </div>

              {entityFilter === 'lead' ? (
                <>
                  <div className="field">
                    <label>Категория клиента</label>
                    <select value={leadCategories[0] || ''} onChange={(event) => setLeadCategories(event.target.value ? [event.target.value] : [])}>
                      <option value="">Любая</option>
                      {['A', 'B', 'C', 'D', 'E', 'unknown'].map((value) => <option value={value} key={value}>{value === 'unknown' ? 'Unknown' : value}</option>)}
                    </select>
                  </div>
                  <div className="field">
                    <label>BANT</label>
                    <select value={bantFilter} onChange={(event) => setBantFilter(event.target.value as CandidateFilter['bant_filter'])}>
                      <option value="">Любой</option>
                      <option value="complete">Полный BANT</option>
                      <option value="incomplete">Неполный BANT</option>
                      <option value="budget">Не подтверждён Budget</option>
                      <option value="authority">Не подтверждён Authority</option>
                      <option value="need">Не подтверждён Need</option>
                      <option value="timeframe">Не подтверждён Timeframe</option>
                      <option value="negative">Есть отрицательный ответ</option>
                      <option value="unknown">Есть критерий без данных</option>
                    </select>
                    <span className="muted">BANT-фильтр применяется только к анализам с четырьмя структурированными критериями.</span>
                  </div>
                </>
              ) : null}

              {entityFilter === 'deal' && (
                <div className="field field-multi">
                  <label>Воронки</label>
                  <button
                    className="btn ghost picker-toggle"
                    type="button"
                    onClick={() => setShowPipelinePicker((value) => !value)}
                  >
                    {pipelineIds.length ? `Выбрано воронок: ${pipelineIds.length}` : 'Выбрать воронки'}
                  </button>
                  <div className="picker-summary">
                    {shortListSummary(selectedPipelineNames(), 'Выберите одну или несколько воронок')}
                  </div>
                  {showPipelinePicker && (
                    <div className="multi-check">
                      {dealPipelines.map((pipeline) => (
                        <label key={pipeline.id} className="check-row">
                          <input
                            type="checkbox"
                            checked={pipelineIds.includes(pipeline.id)}
                            onChange={() => {
                              const nextPipelines = toggleId(pipelineIds, pipeline.id)
                              setPipelineIds(nextPipelines)
                              // Убираем этапы из снятых воронок.
                              const allowed = new Set(
                                dealPipelines
                                  .filter((item) => nextPipelines.includes(item.id))
                                  .flatMap((item) => (item.stages || []).map((stage) => stage.id)),
                              )
                              setStageIds((prev) => prev.filter((id) => allowed.has(id)))
                            }}
                          />
                          <span>{pipeline.name}</span>
                        </label>
                      ))}
                      {!dealPipelines.length && <span className="muted">Справочник воронок пуст</span>}
                    </div>
                  )}
                </div>
              )}

              <div className="field field-multi">
                <label>Этапы</label>
                <button
                  className="btn ghost picker-toggle"
                  type="button"
                  onClick={() => setShowStagePicker((value) => !value)}
                >
                  {stageIds.length
                    ? `Выбрано этапов: ${stageIds.length}`
                    : entityFilter === 'deal' && !pipelineIds.length
                      ? 'Сначала выберите воронку'
                      : 'Выбрать этапы'}
                </button>
                <div className="picker-summary">{shortListSummary(selectedStageNames(), 'Этапы ещё не выбраны')}</div>
                {showStagePicker && (
                  <div className="multi-check">
                    {availableStages().map((stage) => (
                      <label key={stage.id} className="check-row">
                        <input
                          type="checkbox"
                          checked={stageIds.includes(stage.id)}
                          onChange={() => setStageIds((prev) => toggleId(prev, stage.id))}
                        />
                        <span>{stage.name}</span>
                      </label>
                    ))}
                    {!availableStages().length && (
                      <span className="muted">
                        {entityFilter === 'deal' && !pipelineIds.length
                          ? 'Сначала выберите воронку'
                          : 'Этапы не найдены'}
                      </span>
                    )}
                  </div>
                )}
              </div>

              </div>
            )}
            {candidatesError && (
              <div className="alert error">
                <strong>Не удалось загрузить кандидатов:</strong> {candidatesError}
              </div>
            )}
            {!candidatesError && candidatesData && candidatesData.ready === false && (
              <div className="alert">
                {candidatesData.ready_message || 'Выберите воронку и этапы, затем нажмите «Обновить».'}
              </div>
            )}
            {(candidatesData?.candidates || []).map((item) => (
              <article
                key={`${item.entity_type}-${item.entity_id}`}
                className={`deal ${selectedCandidate?.entity_id === item.entity_id && selectedCandidate.entity_type === item.entity_type ? 'active' : ''}`}
              >
                <button className="deal-select" onClick={() => setSelectedCandidate(item)}>
                  <span className="deal-title-row">
                    <strong>
                      {item.entity_type === 'deal' ? 'Сделка' : 'Лид'} {item.entity_id} · {item.client_name || item.title}
                    </strong>
                    {item.analyzed ? <span className="analysis-marker">Есть анализ</span> : null}
                    {item.review_state === 'reviewed' || item.review_state === 'snoozed' ? (
                      <span className="analysis-marker review-marker">Проверено РОПом</span>
                    ) : null}
                    {item.review_state === 'changed' ? (
                      <span className="analysis-marker changed-marker">Повторить: {item.review_change_reason}</span>
                    ) : null}
                  </span>
                  <small>
                    {formatMoneyText(item.status)}
                    {item.amount ? ` · ${formatMoney(item.amount)}` : ''}
                    <br />
                    {formatMoneyText(item.attention_reason)}
                    {item.crm_updated_after_review ? <><br />CRM обновлена после решения РОПа</> : null}
                  </small>
                  {item.entity_type === 'lead' ? <LeadQualificationStrip summary={item.lead_qualification} hasAnalysis={Boolean(item.lead_analysis_available || item.analyzed)} category={item.lead_category} /> : null}
                  <span className={`priority ${item.priority}`}>{priorityLabelRu(item.priority)}</span>
                </button>
                {item.analyzed ? (
                  <button className="deal-report" onClick={() => void openCandidateReport(item)}>
                    Отчёт
                  </button>
                ) : null}
              </article>
            ))}
            {!candidatesLoading &&
              candidatesData?.ready !== false &&
              !candidatesData?.candidates?.length &&
              !candidatesError && <p className="muted">Кандидатов за выбранный период не найдено.</p>}
            {!candidatesLoading && !candidatesData && !candidatesError && (
              <p className="muted">Откройте фильтры, выберите этапы и нажмите «Обновить».</p>
            )}
            <button
              className="btn"
              style={{ marginTop: 10 }}
              disabled={!selectedCandidate || (job?.status === 'running' || job?.status === 'queued')}
              onClick={() => {
                if (!selectedCandidate) return
                void runAnalyze(selectedCandidate.entity_id, selectedCandidate.entity_type)
              }}
            >
              {selectedCandidate?.analyzed ? 'Обновить анализ выбранного' : 'Запустить анализ выбранного'}
            </button>
          </aside>

          <main>
            <section className="section control-strip">
              <div className="cards">
                <div className="card">
                  <div className="label">В очереди</div>
                  <div className="value">{summary?.returned ?? '—'}</div>
                </div>
                <div className="card">
                  <div className="label">Высокий риск</div>
                  <div className="value">{summary?.high ?? '—'}</div>
                </div>
                <div className="card">
                  <div className="label">Средний</div>
                  <div className="value">{summary?.medium ?? '—'}</div>
                </div>
                <div className="card">
                  <div className="label">Скрыто РОПом</div>
                  <div className="value">{summary?.reviewed_hidden ?? '—'}</div>
                </div>
              </div>
            </section>

            <ReportPanels
              meta={activeMeta}
              reportDetail={selectedReport?.id === activeMeta?.report_id ? selectedReport : null}
              leadWorkspaceOpen={leadWorkspaceOpen}
              onCloseLeadWorkspace={closeLeadWorkspace}
              analysis={activeAnalysis}
              facts={facts}
              unknowns={unknowns}
              closureReasons={closureReasons}
              internalChecks={internalChecks}
              recommendations={recommendations}
              managerBrief={managerBrief}
              showMarkdown={showMarkdown}
              markdown={markdown}
              onCopy={() => {
                void navigator.clipboard?.writeText(copyText)
                toast('Задача менеджеру скопирована', setToastMessage)
              }}
              onToggleMarkdown={() => void toggleMarkdown()}
              onDecision={(value) => void onDecision(value)}
              onOutcome={(value) => void onOutcome(value)}
              onQualificationReview={(payload) => void onQualificationReview(payload)}
              qualificationReviews={selectedReport?.qualification_reviews}
              decisions={selectedReport?.decisions}
              outcomes={selectedReport?.outcomes}
            />
          </main>
        </div>
      )}

      {tab === 'manual' && (
        <div className="grid">
          <aside className="panel">
            <h3>Ручной запуск</h3>
            <p>Для срочной проверки конкретного лида или сделки. ID может быть вне активного профиля и не попадёт в snapshot сводки автоматически.</p>
            <div className="field">
              <label>ID или ссылка Bitrix</label>
              <textarea
                value={manualIds}
                onChange={(e) => {
                  const value = e.target.value
                  const parsed = parseManualInput(value, options.entity_type)
                  setManualIds(value)
                  if (parsed.detected.length && !parsed.error) {
                    setOptions((prev) => ({ ...prev, entity_type: parsed.entityType }))
                  }
                }}
                placeholder={'18457, 18533\nhttps://…/crm/deal/details/18619/\nhttps://…/crm/lead/details/228505/'}
              />
              {manualInput.detected.length && !manualInput.error ? (
                <span className="input-hint">
                  Распознано: {manualInput.entityType === 'deal' ? 'сделка' : 'лид'} · {manualInput.ids.join(', ')}
                </span>
              ) : null}
              {manualInput.error ? <span className="input-error">{manualInput.error}</span> : null}
            </div>
            <div className="filters manual-basic">
              <div className="field">
                <label>Тип</label>
                <select
                  value={options.entity_type}
                  onChange={(e) =>
                    setOptions((prev) => ({ ...prev, entity_type: e.target.value as AnalyzeOptions['entity_type'] }))
                  }
                >
                  <option value="auto">Авто (сначала лид)</option>
                  <option value="lead">Лид</option>
                  <option value="deal">Сделка</option>
                </select>
              </div>
            </div>
            <button
              className="btn"
              onClick={() => void runAnalyze(manualInput.ids.join(', '), manualInput.entityType)}
              disabled={
                !manualInput.ids.length ||
                Boolean(manualInput.error) ||
                job?.status === 'running' ||
                job?.status === 'queued'
              }
            >
              Запустить анализ
            </button>
            <button
              className="btn ghost advanced-toggle"
              type="button"
              onClick={() => setShowManualAdvanced((value) => !value)}
            >
              {showManualAdvanced ? 'Скрыть параметры запуска' : 'Параметры запуска'}
            </button>
            {showManualAdvanced && (
              <>
                <div className="filters manual-advanced">
              <div className="field">
                <label>История, дней</label>
                <input
                  type="number"
                  min={0}
                  value={options.history_days}
                  onChange={(e) =>
                    setOptions((prev) => ({ ...prev, history_days: Math.max(0, Number(e.target.value) || 0) }))
                  }
                />
              </div>
              <div className="field">
                <label>Транскрипты</label>
                <select
                  value={options.transcript_mode}
                  onChange={(e) =>
                    setOptions((prev) => ({
                      ...prev,
                      transcript_mode: e.target.value as AnalyzeOptions['transcript_mode'],
                    }))
                  }
                >
                  <option value="all">all</option>
                  <option value="latest">latest</option>
                  <option value="none">none</option>
                </select>
              </div>
                </div>
                <div className="checks">
                  {(
                    [
                      ['include_related', 'Связанные сделки контакта'],
                      ['include_internal', 'Внутренний контекст'],
                      ['download_audio', 'Скачать недостающие аудио'],
                      ['redownload_audio', 'Перекачать аудио'],
                      ['transcribe_audio', 'Транскрибировать (кроме <20 сек)'],
                      ['analyze', 'Запустить LLM-анализ'],
                      ['force_llm', 'Принудительный полный LLM'],
                    ] as const
                  ).map(([key, label]) => (
                    <label key={key}>
                      <input
                        type="checkbox"
                        checked={Boolean(options[key])}
                        onChange={(e) => setOptions((prev) => ({ ...prev, [key]: e.target.checked }))}
                      />
                      {label}
                    </label>
                  ))}
                </div>
              </>
            )}
            {jobError && (
              <div className="alert error" style={{ marginTop: 12 }}>
                <strong>Ошибка:</strong> {jobError}
              </div>
            )}
            {job && (
              <div className="job-panel">
                <h3>Прогресс</h3>
                <p className="muted">
                  {job.status === 'running' || job.status === 'queued'
                    ? 'Анализ может занять несколько минут: скачиваются аудио, собирается история и запускается LLM.'
                    : `job ${job.job_id} · ${job.status}`}
                </p>
                <div className="pipeline" style={{ gridTemplateColumns: '1fr' }}>
                  {job.stages.map((stage) => (
                    <div key={stage.key} className={`step ${stage.status}`}>
                      <b>{stage.label}</b>
                      <span className="muted">{stage.status}</span>
                      {stage.detail ? <div className="muted">{stage.detail}</div> : null}
                    </div>
                  ))}
                </div>
                {!!job.logs?.length && (
                  <details className="job-log">
                    <summary>Лог выполнения ({job.logs.length})</summary>
                    <pre>{job.logs.join('\n')}</pre>
                  </details>
                )}
                {!!job.results?.length && (
                  <>
                    <h3 style={{ marginTop: 16 }}>Результаты</h3>
                    {job.results.map((result, index) => (
                      <button
                        key={`${result.entity_type}-${result.entity_id}`}
                        className={`deal ${selectedResultIndex === index ? 'active' : ''}`}
                        onClick={() => setSelectedResultIndex(index)}
                      >
                        <strong>
                          {result.entity_type} {result.entity_id}
                        </strong>
                        <small>{result.attention_reason || (result.has_analysis ? 'анализ готов' : 'нет analysis')}</small>
                        {result.entity_type === 'lead' ? <LeadQualificationStrip summary={result.lead_qualification} hasAnalysis={result.has_analysis} category={result.lead_category} /> : null}
                      </button>
                    ))}
                  </>
                )}
              </div>
            )}
          </aside>
          <main>
            <ReportPanels
              meta={activeMeta}
              reportDetail={selectedReport?.id === activeMeta?.report_id ? selectedReport : null}
              leadWorkspaceOpen={leadWorkspaceOpen}
              onCloseLeadWorkspace={closeLeadWorkspace}
              analysis={activeAnalysis}
              facts={facts}
              unknowns={unknowns}
              closureReasons={closureReasons}
              internalChecks={internalChecks}
              recommendations={recommendations}
              managerBrief={managerBrief}
              showMarkdown={showMarkdown}
              markdown={markdown}
              onCopy={() => {
                void navigator.clipboard?.writeText(copyText)
                toast('Задача менеджеру скопирована', setToastMessage)
              }}
              onToggleMarkdown={() => void toggleMarkdown()}
              onDecision={(value) => void onDecision(value)}
              onOutcome={(value) => void onOutcome(value)}
              onQualificationReview={(payload) => void onQualificationReview(payload)}
              qualificationReviews={selectedReport?.qualification_reviews}
            />
          </main>
        </div>
      )}

      {tab === 'history' && (
        <div className="grid">
          <aside className="panel">
            <h3>История отчётов</h3>
            <p>Сохранённые анализы и решения РОПа.</p>
            <button className="btn secondary" onClick={() => void loadHistory(true)}>
              Обновить
            </button>
            {historyError && (
              <div className="alert error" style={{ marginTop: 12 }}>
                <strong>Ошибка:</strong> {historyError}
              </div>
            )}
            <div style={{ marginTop: 12 }}>
              {history.map((item) => (
                <button
                  key={item.id}
                  className={`deal ${selectedReport?.id === item.id ? 'active' : ''}`}
                  onClick={() => void openHistoryReport(item.id)}
                >
                  <strong>
                    #{item.id} · {item.entity_type} {item.entity_id}
                  </strong>
                  <small>{reportPreview(item)}</small>
                  {item.entity_type === 'lead' ? <LeadQualificationStrip summary={item.lead_qualification} hasAnalysis category={item.lead_category} /> : null}
                </button>
              ))}
              {!history.length && !historyError && <p className="muted">Пока нет сохранённых отчётов.</p>}
            </div>
          </aside>
          <main>
            <ReportPanels
              meta={activeMeta}
              reportDetail={selectedReport?.id === activeMeta?.report_id ? selectedReport : null}
              leadWorkspaceOpen={leadWorkspaceOpen}
              onCloseLeadWorkspace={closeLeadWorkspace}
              analysis={activeAnalysis}
              facts={facts}
              unknowns={unknowns}
              closureReasons={closureReasons}
              internalChecks={internalChecks}
              recommendations={recommendations}
              managerBrief={managerBrief}
              showMarkdown={showMarkdown}
              markdown={markdown}
              onCopy={() => {
                void navigator.clipboard?.writeText(copyText)
                toast('Задача менеджеру скопирована', setToastMessage)
              }}
              onToggleMarkdown={() => void toggleMarkdown()}
              onDecision={(value) => void onDecision(value)}
              onOutcome={(value) => void onOutcome(value)}
              onQualificationReview={(payload) => void onQualificationReview(payload)}
              qualificationReviews={selectedReport?.qualification_reviews}
              decisions={selectedReport?.decisions}
              outcomes={selectedReport?.outcomes}
            />
          </main>
        </div>
      )}

      {toastMessage && <div className="toast">{toastMessage}</div>}
    </div>
  )
}

type ReportPanelsProps = {
  meta: {
    entity_type: string
    entity_id: string
    report_id: number | null
    risk_level?: string | null
    attention_reason?: string | null
    recommended_action?: string | null
    bitrix_url?: string | null
    candidate_review?: Record<string, unknown> | null
  } | null
  reportDetail?: UiReportDetail | null
  leadWorkspaceOpen: boolean
  onCloseLeadWorkspace: () => void
  analysis: Record<string, unknown> | null
  facts: string[]
  unknowns: string[]
  closureReasons: string[]
  internalChecks: string[]
  recommendations: string[]
  managerBrief: ManagerBrief
  showMarkdown: boolean
  markdown: string | null
  onCopy: () => void
  onToggleMarkdown: () => void
  onDecision: (value: string) => void
  onOutcome: (value: string) => void
  onQualificationReview: (payload: {
    is_correct: boolean
    issue_fields?: string[]
    corrected_statuses?: Record<string, string>
    corrected_category?: string | null
    comment?: string | null
  }) => void
  decisions?: Array<Record<string, unknown>>
  outcomes?: Array<Record<string, unknown>>
  qualificationReviews?: Array<Record<string, unknown>>
}

function ReportPanels(props: ReportPanelsProps) {
  const [analysisTab, setAnalysisTab] = useState<'current' | 'compact' | 'comparison'>('current')
  if (props.meta?.entity_type === 'lead' && props.analysis) {
    if (!props.leadWorkspaceOpen) return null
    return createPortal(
      <div className="lead-workspace-backdrop" onMouseDown={(event) => {
        if (event.target === event.currentTarget) props.onCloseLeadWorkspace()
      }}>
        <section className="lead-workspace-window" role="dialog" aria-modal="true" aria-label={`Карточка лида ${props.meta.entity_id}`}>
          <LeadWorkflowPanels {...props} />
        </section>
      </div>,
      document.body,
    )
  }
  return (
    <>
      <section className="section analysis-tabs">
        <div className="tab-row" role="tablist" aria-label="Анализ карточки">
          <button className={analysisTab === 'current' ? 'active' : ''} onClick={() => setAnalysisTab('current')}>
            Текущий анализ
          </button>
          <button className={analysisTab === 'compact' ? 'active' : ''} onClick={() => setAnalysisTab('compact')}>
            Compact beta
          </button>
          <button className={analysisTab === 'comparison' ? 'active' : ''} onClick={() => setAnalysisTab('comparison')}>
            Сравнение
          </button>
        </div>
      </section>
      {analysisTab === 'current' ? (
        <FullAnalysisPanels {...props} />
      ) : (
        <CompactReviewPanel meta={props.meta} fullAnalysis={props.analysis} comparison={analysisTab === 'comparison'} />
      )}
    </>
  )
}

type LeadMaterialTab = 'summary' | 'bant' | 'evidence' | 'audit' | 'history' | 'technical'

function formatLeadDate(value: string | null | undefined, includeTime = true): string {
  if (!value) return 'дата не указана'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('ru-RU', includeTime
    ? { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { day: '2-digit', month: '2-digit', year: 'numeric' })
}

function LeadWorkflowPanels(props: ReportPanelsProps) {
  const { meta, analysis, reportDetail, onCloseLeadWorkspace } = props
  const [workflow, setWorkflow] = useState<LeadWorkflowState | null>(reportDetail?.workflow || null)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState('')
  const [materialTab, setMaterialTab] = useState<LeadMaterialTab | null>(null)
  const [qualificationIssues, setQualificationIssues] = useState<string[]>([])
  const [qualificationComment, setQualificationComment] = useState('')

  useEffect(() => {
    setWorkflow(reportDetail?.workflow || null)
    setMaterialTab(null)
  }, [reportDetail?.id, reportDetail?.workflow])

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (materialTab) setMaterialTab(null)
      else onCloseLeadWorkspace()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [materialTab, onCloseLeadWorkspace])

  const leadState = asRecord(analysis?.lead_state)
  const rop = asRecord(analysis?.rop_manager_message_block)
  const action = asRecord(analysis?.manager_action_block)
  const assessment = asRecord(analysis?.qualification_assessment)
  const bant = asRecord(assessment.bant)
  const category = asRecord(assessment.lead_category)
  const route = asRecord(assessment.lead_route)
  const solutionFit = asRecord(assessment.solution_fit)
  const commercialFit = asRecord(assessment.commercial_fit)
  const reportMeta = reportDetail?.report_meta || {}
  const bantItems = [
    { key: 'budget', letter: 'B', label: 'Бюджет' },
    { key: 'authority', letter: 'A', label: 'ЛПР' },
    { key: 'need', letter: 'N', label: 'Потребность' },
    { key: 'timeframe', letter: 'T', label: 'Сроки' },
  ].map((item) => ({ ...item, value: asRecord(bant[item.key]) }))
  const confirmedBant = bantItems.filter((item) => asString(item.value.status) === 'confirmed').length
  const bantPercent = Math.round((confirmedBant / 4) * 100)
  const client = asString(leadState.client) || asString(reportMeta.client_name) || 'Нет данных'
  const categoryValue = asString(category.value) || asString(leadState.qualification) || 'Unknown'
  const interest = formatMoneyText(asString(leadState.need))
  const latestQualificationReview = props.qualificationReviews?.[0]

  async function persist(patch: Partial<LeadWorkflowState>) {
    if (!meta?.report_id || !workflow) return
    setSaving(true)
    setNotice('')
    try {
      const saved = await saveLeadWorkflow(meta.entity_id, {
        ...patch,
        source_report_id: workflow.source_report_id || meta.report_id,
      })
      setWorkflow(saved)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Не удалось сохранить workflow')
    } finally {
      setSaving(false)
    }
  }

  function updateDraft(field: 'manager_review_text' | 'manager_task_text', value: string) {
    setWorkflow((current) => current ? { ...current, [field]: value } : current)
  }

  function copyValue(value: string, label: string) {
    void navigator.clipboard?.writeText(value)
    setNotice(`${label} скопирован`)
  }

  function developmentStub() {
    setNotice('Функция в разработке')
  }

  function openMaterials(tab: LeadMaterialTab) {
    setMaterialTab(tab)
    if (tab === 'audit' && !props.showMarkdown && reportDetail?.markdown_available) {
      props.onToggleMarkdown()
    }
  }

  if (!workflow || !meta?.report_id) {
    return (
      <section className="section lead-workflow-empty">
        <h2>Лид {meta?.entity_id}</h2>
        <p>Workflow станет доступен после сохранения отчёта. Аналитические данные уже можно просматривать в текущем отчёте.</p>
      </section>
    )
  }

  const lastContact = reportMeta.last_contact
  const currentTask = reportMeta.current_task
  const taskEnabled = workflow.review_completed
  const controlEnabled = workflow.task_completed
  const finalEnabled = workflow.control_completed
  const workflowDone = [workflow.review_completed, workflow.task_completed, workflow.control_completed, Boolean(workflow.final_decision)].filter(Boolean).length
  const evidence = [
    ...props.facts,
    ...asStringList(rop.evidence).map(formatMoneyText),
    ...bantItems.flatMap((item) => asStringList(item.value.evidence).map(formatMoneyText)),
  ].filter(Boolean)

  return (
    <>
      <section className="lead-workflow-shell" id="report">
        <header className="lead-workflow-header">
          <div className="lead-workflow-heading">
            <div className="lead-title-line">
              <h2>Лид #{meta.entity_id}</h2>
              <span className="attention-badge">Требует внимания РОПа</span>
              <span className="workflow-status">{workflow.status_label}</span>
            </div>
            <div className="lead-meta-line">
              <span>Клиент: {client}</span>
              {reportMeta.manager_id ? <span>Менеджер: ID {reportMeta.manager_id}</span> : null}
              <span>Этап: {reportMeta.stage_name || 'Нет данных'}</span>
              <span>Категория: {categoryValue}</span>
              {meta.risk_level ? <span>Риск: {riskLabelRu(meta.risk_level)}</span> : null}
              <span>Анализ: {formatLeadDate(reportDetail?.created_at)}</span>
            </div>
            {interest ? <div className="lead-context-line"><strong>Интерес:</strong> {interest}</div> : null}
          </div>
          <div className="lead-header-actions">
            {meta.bitrix_url ? <a href={meta.bitrix_url} target="_blank" rel="noreferrer">Открыть в Bitrix</a> : null}
            <button onClick={() => openMaterials('bant')}>Полный BANT</button>
            <button onClick={() => openMaterials('summary')}>Материалы анализа</button>
            <button className="workspace-close" onClick={onCloseLeadWorkspace} aria-label="Закрыть карточку лида">Закрыть</button>
          </div>
        </header>

        <section className="lead-activity-strip" aria-label="Последняя активность в CRM">
          <div className="lead-activity-card">
            {lastContact ? (
              <details>
                <summary><strong>Последний контакт</strong><span>{lastContact.type || 'контакт'} · {formatLeadDate(lastContact.date)}{lastContact.subject ? ` · ${lastContact.subject}` : ''}</span></summary>
                <p>{lastContact.text || 'Дополнительного текста нет.'}</p>
              </details>
            ) : <div><strong>Последний контакт</strong><span className="muted">Нет данных</span></div>}
          </div>
          <div className="lead-activity-card">
            {currentTask ? (
              <details>
                <summary><strong>{currentTask.completed ? 'Последняя задача' : 'Актуальная задача'}</strong><span>{formatLeadDate(currentTask.date)}{currentTask.subject ? ` · ${currentTask.subject}` : ''}</span></summary>
                <p>{currentTask.text || 'Дополнительного текста нет.'}</p>
              </details>
            ) : <div><strong>Актуальная задача</strong><span className="muted">Нет данных</span></div>}
          </div>
        </section>

        <section className="compact-bant" aria-label="Квалификация BANT">
          <div className="workflow-section-title">
            <div><h3>Квалификация BANT</h3><p>{confirmedBant} из 4 факторов подтверждены</p></div>
            <span>{bantPercent}%</span>
          </div>
          <div className="compact-bant-grid">
            {bantItems.map((item) => {
              const status = asString(item.value.status, 'unknown')
              const summary = formatMoneyText(asString(item.value.summary) || asString(item.value.explanation))
              return (
                <button className={`compact-bant-card status-${status}`} key={item.key} onClick={() => openMaterials('bant')}>
                  <strong>{item.letter} · {item.label}</strong>
                  <span>{assessmentLabel(status, BANT_STATUS_RU)}</span>
                  {summary ? <small>{summary}</small> : null}
                </button>
              )
            })}
            <div className="compact-bant-total"><strong>Итого · {bantPercent}%</strong><span>{confirmedBant} из 4 подтверждены</span></div>
          </div>
        </section>

        <section className="workflow-steps">
          <div className="workflow-section-title">
            <div><h3>Работа РОПа по лиду</h3><p>Разобрать → поручить → проверить → принять решение</p></div>
            <span>{workflowDone} из 4 выполнено</span>
          </div>

          <article className={`workflow-step ${workflow.review_completed ? 'completed' : 'active'}`}>
            <div className="workflow-step-label"><b>1</b><strong>Разбор с менеджером</strong><span>Сильные и слабые стороны</span></div>
            <div className="workflow-step-body">
              <p><strong>Цель:</strong> разобрать обработку лида и показать менеджеру, что нужно усилить.</p>
              <div className="workflow-tags"><button onClick={() => openMaterials('evidence')}>Недожатые BANT-факторы</button><button onClick={() => openMaterials('evidence')}>Основание</button></div>
              <label>Текст разбора менеджеру</label>
              <textarea value={workflow.manager_review_text || ''} onChange={(event) => updateDraft('manager_review_text', event.target.value)} onBlur={() => void persist({ manager_review_text: workflow.manager_review_text })} />
            </div>
            <div className="workflow-step-actions">
              <button onClick={() => copyValue(workflow.manager_review_text || '', 'Разбор')}>Копировать разбор</button>
              <button className="primary-dark" onClick={developmentStub}>Отправить в Bitrix</button>
              <label><input type="checkbox" checked={workflow.review_completed} disabled={saving} onChange={(event) => void persist({ review_completed: event.target.checked })} /> Выполнено</label>
            </div>
          </article>

          <article className={`workflow-step ${workflow.task_completed ? 'completed' : taskEnabled ? 'active' : 'disabled'}`}>
            <div className="workflow-step-label"><b>2</b><strong>Поставить задачу менеджеру</strong><span>Фиксация и план</span></div>
            <div className="workflow-step-body">
              <p><strong>Цель:</strong> дать менеджеру конкретное поручение для закрытия недостающих фактов.</p>
              <label>Что нужно сделать</label>
              <textarea disabled={!taskEnabled} value={workflow.manager_task_text || ''} onChange={(event) => updateDraft('manager_task_text', event.target.value)} onBlur={() => void persist({ manager_task_text: workflow.manager_task_text })} />
            </div>
            <div className="workflow-step-actions">
              <button disabled={!taskEnabled} onClick={() => copyValue(workflow.manager_task_text || '', 'Задача')}>Копировать задачу</button>
              <button disabled={!taskEnabled} className="primary-dark" onClick={developmentStub}>Поставить задачу</button>
              <label><input type="checkbox" checked={workflow.task_completed} disabled={!taskEnabled || saving} onChange={(event) => void persist({ task_completed: event.target.checked })} /> Выполнено вручную</label>
            </div>
          </article>

          <article className={`workflow-step ${workflow.control_completed ? 'completed' : controlEnabled ? 'active' : 'disabled'}`}>
            <div className="workflow-step-label"><b>3</b><strong>Контроль и проверка</strong><span>Проверяем результат</span></div>
            <div className="workflow-step-body">
              <p><strong>Цель:</strong> проверить выполнение поручения и наличие результата контакта.</p>
              <div className="control-checklist">
                {asStringList(action.manager_checklist).length ? asStringList(action.manager_checklist).map((item) => <span key={item}>• {formatMoneyText(item)}</span>) : <span>{formatMoneyText(asString(rop.check_for_rop)) || 'Проверить результат в CRM.'}</span>}
              </div>
              <div className="control-fields">
                <label>Режим<select disabled={!controlEnabled} value={workflow.control_mode || 'days'} onChange={(event) => setWorkflow({ ...workflow, control_mode: event.target.value as LeadWorkflowState['control_mode'] })}><option value="days">Через N дней</option><option value="date">Точная дата</option><option value="daily">Ежедневно / VIP</option></select></label>
                {workflow.control_mode !== 'date' && workflow.control_mode !== 'daily' ? <label>Дней<input disabled={!controlEnabled} type="number" min="1" max="365" value={workflow.control_days || 2} onChange={(event) => setWorkflow({ ...workflow, control_days: Number(event.target.value) })} /></label> : null}
                {workflow.control_mode === 'date' ? <label>Дата<input disabled={!controlEnabled} type="date" value={workflow.control_date || ''} onChange={(event) => setWorkflow({ ...workflow, control_date: event.target.value })} /></label> : null}
                <button disabled={!controlEnabled || saving} onClick={() => void persist({ control_mode: workflow.control_mode || 'days', control_days: workflow.control_days || 2, control_date: workflow.control_date })}>Поставить контроль</button>
              </div>
            </div>
            <div className="workflow-step-actions">
              <button disabled={!controlEnabled} onClick={() => openMaterials('evidence')}>Основание проверки</button>
              <label><input type="checkbox" checked={workflow.control_completed} disabled={!controlEnabled || !workflow.control_mode || saving} onChange={(event) => void persist({ control_completed: event.target.checked })} /> Проверено</label>
            </div>
          </article>

          <article className={`workflow-step ${workflow.final_decision ? 'completed' : finalEnabled ? 'active' : 'disabled'}`}>
            <div className="workflow-step-label"><b>4</b><strong>Финальный статус</strong><span>Решение по лиду</span></div>
            <div className="workflow-step-body">
              <p><strong>Цель:</strong> принять решение после проверки результата.</p>
              <p>Продолжать работу — если лид актуален и есть следующий шаг.</p>
              <p>Не требует внимания — если лид неактуален или не соответствует критериям.</p>
            </div>
            <div className="workflow-step-actions">
              <button disabled={!finalEnabled || saving} className={workflow.final_decision === 'continue' ? 'primary-dark' : ''} onClick={() => void persist({ final_decision: 'continue' })}>Продолжать работу</button>
              <button disabled={!finalEnabled || saving} onClick={() => void persist({ final_decision: 'no_attention' })}>Не требует внимания</button>
              {!finalEnabled ? <span className="muted">Доступно после проверки</span> : null}
            </div>
          </article>
        </section>
        {notice ? <div className="workflow-notice" role="status">{notice}</div> : null}
      </section>

      {materialTab ? (
        <div className="analysis-drawer-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) setMaterialTab(null) }}>
          <aside className="analysis-drawer" aria-label="Материалы анализа">
            <header><div><h2>Материалы анализа</h2><p>Лид #{meta.entity_id} · отчёт #{reportDetail?.id}</p></div><button onClick={() => setMaterialTab(null)}>Закрыть</button></header>
            <nav className="analysis-drawer-tabs">
              {([
                ['summary', 'Краткий вывод'], ['bant', 'Полный BANT'], ['evidence', 'Доказательства'],
                ['audit', 'Аудитный отчёт'], ['history', 'История'], ['technical', 'Техническая информация'],
              ] as Array<[LeadMaterialTab, string]>).map(([key, label]) => <button className={materialTab === key ? 'active' : ''} onClick={() => openMaterials(key)} key={key}>{label}</button>)}
            </nav>
            <div className="analysis-drawer-content">
              {materialTab === 'summary' ? <div className="material-summary"><h3>Краткий вывод</h3><p><strong>Вывод:</strong> {formatMoneyText(asString(leadState.summary)) || meta.attention_reason || 'Нет данных'}</p><p><strong>Поручение:</strong> {formatMoneyText(asString(rop.message_to_manager)) || workflow.manager_task_text || 'Нет данных'}</p><p><strong>Критерий проверки:</strong> {formatMoneyText(asString(rop.success_condition)) || 'Нет данных'}</p></div> : null}
              {materialTab === 'bant' ? <div className="material-bant"><h3>Полный BANT</h3>{bantItems.map((item) => <article key={item.key}><h4>{item.letter} · {item.label} — {assessmentLabel(asString(item.value.status, 'unknown'), BANT_STATUS_RU)}</h4><p>{formatMoneyText(asString(item.value.summary) || asString(item.value.explanation)) || 'Пояснение отсутствует'}</p><b>Доказательства</b><ul>{asStringList(item.value.evidence).map((text) => <li key={text}>{formatMoneyText(text)}</li>)}{!asStringList(item.value.evidence).length ? <li>Нет подтверждённых фактов</li> : null}</ul><b>Чего не хватает</b><ul>{asStringList(item.value.missing_facts).map((text) => <li key={text}>{formatMoneyText(text)}</li>)}{!asStringList(item.value.missing_facts).length ? <li>Не указано</li> : null}</ul></article>)}<article><h4>Категория {categoryValue}</h4><p>{formatMoneyText(asString(category.reason)) || 'Обоснование отсутствует'}</p><p><strong>Маршрут:</strong> {assessmentLabel(asString(route.status, 'unknown'), LEAD_ROUTE_STATUS_RU)}</p><p><strong>Техническая применимость:</strong> {assessmentLabel(asString(solutionFit.status, 'unknown'), SOLUTION_FIT_STATUS_RU)}</p><p><strong>Бюджет нового оборудования:</strong> {assessmentLabel(asString(commercialFit.new_equipment_budget_status, 'unknown'), COMMERCIAL_FIT_STATUS_RU)}</p></article><div className="qualification-feedback"><h4>Проверка РОПом</h4>{latestQualificationReview ? <p className="muted">Последняя проверка: {latestQualificationReview.is_correct ? 'оценка верна' : 'есть исправления'} · {asString(latestQualificationReview.created_at)}</p> : null}<button onClick={() => props.onQualificationReview({ is_correct: true })}>Оценка верна</button><div className="qualification-issue-grid">{Object.entries(QUALIFICATION_ISSUE_LABELS).map(([key, label]) => <label key={key}><input type="checkbox" checked={qualificationIssues.includes(key)} onChange={() => setQualificationIssues((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key])} />{label}</label>)}</div><textarea placeholder="Как должно быть и на каком факте это основано" value={qualificationComment} onChange={(event) => setQualificationComment(event.target.value)} /><button disabled={!qualificationIssues.length} onClick={() => { props.onQualificationReview({ is_correct: false, issue_fields: qualificationIssues, comment: qualificationComment || null }); setQualificationIssues([]); setQualificationComment('') }}>Сохранить исправление</button></div></div> : null}
              {materialTab === 'evidence' ? <div><h3>Доказательства</h3><ul>{[...new Set(evidence)].map((item) => <li key={item}>{item}</li>)}{!evidence.length ? <li>Доказательства в этом анализе отсутствуют.</li> : null}</ul><h3>Недостающие данные</h3><ul>{props.unknowns.map((item) => <li key={item}>{item}</li>)}{!props.unknowns.length ? <li>Пробелы не выделены.</li> : null}</ul></div> : null}
              {materialTab === 'audit' ? <div><h3>Аудитный отчёт</h3>{!reportDetail?.markdown_available ? <p className="muted">Для этого отчёта Markdown недоступен.</p> : props.markdown ? <div className="markdown">{formatMoneyText(props.markdown)}</div> : <p className="muted">Загрузка отчёта…</p>}</div> : null}
              {materialTab === 'history' ? <div><h3>История анализов и решений</h3><ul className="material-history">{reportDetail?.entity_history?.map((item) => <li key={asString(item.id)}><strong>Отчёт #{asString(item.id)}</strong><span>{asString(item.created_at)} · {riskLabelRu(asString(item.risk_level))}</span><p>{asString(item.attention_reason)}</p></li>)}</ul><h4>Решения РОПа</h4><ul>{props.decisions?.map((item) => <li key={asString(item.id)}>{asString(item.created_at)} · {asString(item.decision)}</li>)}{!props.decisions?.length ? <li>Решений пока нет.</li> : null}</ul><h4>Исходы</h4><ul>{props.outcomes?.map((item) => <li key={asString(item.id)}>{asString(item.checked_at)} · {asString(item.outcome_type)}</li>)}{!props.outcomes?.length ? <li>Исходы пока не зафиксированы.</li> : null}</ul></div> : null}
              {materialTab === 'technical' ? <div><h3>Техническая информация</h3><p><strong>Источник workflow:</strong> отчёт #{workflow.source_report_id || 'не указан'}</p><p><strong>Этап CRM:</strong> {reportMeta.stage_name || reportMeta.stage_id || 'Нет данных'}</p>{reportDetail?.technical_log ? <pre className="technical-log">{JSON.stringify(reportDetail.technical_log, null, 2)}</pre> : <p className="muted">Очищенный технический snapshot отсутствует в старом отчёте.</p>}</div> : null}
            </div>
          </aside>
        </div>
      ) : null}
    </>
  )
}

function FullAnalysisPanels(props: ReportPanelsProps) {
  const [showQualificationCorrection, setShowQualificationCorrection] = useState(false)
  const [qualificationIssueFields, setQualificationIssueFields] = useState<string[]>([])
  const [correctedStatuses, setCorrectedStatuses] = useState<Record<string, string>>({})
  const [correctedCategory, setCorrectedCategory] = useState('')
  const [qualificationComment, setQualificationComment] = useState('')
  const { meta, analysis, facts, unknowns, closureReasons, internalChecks, recommendations, managerBrief } = props
  const isLead = meta?.entity_type === 'lead'
  const dealState = asRecord(analysis?.deal_state)
  const leadState = asRecord(analysis?.lead_state)
  const mainRisk = asRecord(analysis?.main_risk)
  const loss = asRecord(analysis?.loss_diagnosis)
  const rop = asRecord(analysis?.rop_manager_message_block)
  const qualificationAssessment = asRecord(analysis?.qualification_assessment)
  const bant = asRecord(qualificationAssessment.bant)
  const timeframeAssessment = asRecord(bant.timeframe)
  const solutionFit = asRecord(qualificationAssessment.solution_fit)
  const commercialFit = asRecord(qualificationAssessment.commercial_fit)
  const leadCategory = asRecord(qualificationAssessment.lead_category)
  const leadRoute = asRecord(qualificationAssessment.lead_route)
  const dealMode = asRecord(analysis?.deal_mode)
  const priority = asRecord(analysis?.priority_recommendation)

  const riskLevel = asString(mainRisk.risk_level) || asString(meta?.risk_level) || ''
  const riskRu = riskLabelRu(riskLevel)
  const riskType = asString(mainRisk.risk_type)
  const verdict = asString(loss.final_verdict)
  const client = asString(leadState.client) || asString(dealState.client) || '—'
  const qualification = asString(leadCategory.value) || asString(leadState.qualification) || 'Unknown'
  const qualificationReason = asString(leadCategory.reason) || asString(leadState.qualification_reason)
  const hasQualificationAssessment = Object.keys(qualificationAssessment).length > 0
  const bantStatus = assessmentLabel(asString(bant.overall_status), BANT_STATUS_RU)
  const solutionFitStatus = assessmentLabel(asString(solutionFit.status), SOLUTION_FIT_STATUS_RU)
  const commercialFitStatus = assessmentLabel(
    asString(commercialFit.new_equipment_budget_status),
    COMMERCIAL_FIT_STATUS_RU,
  )
  const confirmedBudget = formatMoney(commercialFit.confirmed_budget_rub)
  const minimumBudget = formatMoney(commercialFit.new_equipment_minimum_rub) || '1 000 000 ₽'
  const nextQualificationQuestion = formatMoneyText(asString(bant.next_question))
  const bantItems = [
    { key: 'budget', letter: 'B', fallbackLabel: 'Budget · Бюджет и финансовая готовность' },
    { key: 'authority', letter: 'A', fallbackLabel: 'Authority · ЛПР и влияние на решение' },
    { key: 'need', letter: 'N', fallbackLabel: 'Need · Актуальная потребность' },
    { key: 'timeframe', letter: 'T', fallbackLabel: 'Timeframe · Срок покупки или запуска' },
  ].map((definition) => ({ ...definition, value: asRecord(bant[definition.key]) }))
  const confirmedBantCount = bantItems.filter((item) => asString(item.value.status) === 'confirmed').length
  const categoryBantFactors = asStringList(leadCategory.bant_factors).map(formatMoneyText)
  const categoryTechnicalFactors = asStringList(leadCategory.technical_factors).map(formatMoneyText)
  const categoryBudgetFactors = asStringList(leadCategory.budget_factors).map(formatMoneyText)
  const categoryMissingFacts = asStringList(leadCategory.missing_facts).map(formatMoneyText)
  const categoryNextStep = formatMoneyText(
    asString(leadCategory.next_step) || asString(rop.check_for_rop) || asString(rop.message_to_manager),
  )
  const routeStatus = assessmentLabel(asString(leadRoute.status), LEAD_ROUTE_STATUS_RU)
  const currentRoute = assessmentLabel(asString(leadRoute.current_route), LEAD_ROUTE_RU)
  const recommendedRoute = assessmentLabel(asString(leadRoute.recommended_route), LEAD_ROUTE_RU)
  const amount = formatMoney(dealState.amount)
  const stage = asString(dealState.stage) || '—'
  const attention =
    formatMoneyText(
      asString(meta?.attention_reason) ||
        asString(mainRisk.description) ||
        asString(rop.why_it_matters) ||
        asString(leadState.summary) ||
        asString(dealState.summary) ||
        '—',
    )
  const nextAction =
    formatMoneyText(
      asString(meta?.recommended_action) ||
        asString(rop.check_for_rop) ||
        asString(rop.message_to_manager) ||
        '—',
    )
  const title = meta
    ? `${meta.entity_type === 'deal' ? 'Сделка' : meta.entity_type === 'lead' ? 'Лид' : meta.entity_type} ${meta.entity_id}`
    : 'Выберите кандидата или запустите анализ'
  const bitrixUrl = meta?.bitrix_url || ''
  const needsAttention = Boolean(meta && (riskLevel === 'high' || riskLevel === 'medium_high' || attention !== '—'))
  const hasAnalysis = Boolean(analysis)
  const latestQualificationReview = props.qualificationReviews?.[0]
  const controlledReturnStatus = assessmentLabel(asString(leadRoute.controlled_return_status), CONTROLLED_RETURN_STATUS_RU)
  const controlledReturnExistingDate = asString(leadRoute.controlled_return_date)
  const controlledReturnRecommendedDate = asString(leadRoute.recommended_return_date)
  const ropControlBlock = hasAnalysis ? (
    <section className="rop-control-card" aria-label="Контроль РОПа">
      <div className="rop-control-head">
        <div><div className="hero-label">Управленческое действие</div><h3>Контроль РОПа</h3></div>
        <span>{formatMoneyText(asString(rop.deadline)) || 'срок не указан'}</span>
      </div>
      <div className="rop-control-grid">
        <div><div className="label">Что проверить</div><p>{formatMoneyText(asString(rop.check_for_rop)) || nextAction}</p></div>
        <div><div className="label">Почему важно</div><p>{formatMoneyText(asString(rop.why_it_matters)) || attention}</p></div>
        <div className="rop-control-wide"><div className="label">Поручение менеджеру</div><p>{formatMoneyText(asString(rop.message_to_manager)) || '—'}</p></div>
        <div><div className="label">Ожидаемый факт в CRM</div><p>{formatMoneyText(asString(rop.expected_crm_update)) || '—'}</p></div>
        <div><div className="label">Критерий выполнения</div><p>{formatMoneyText(asString(rop.success_condition)) || '—'}</p></div>
      </div>
      <div className="rop-control-evidence"><div className="label">Основание</div>{asStringList(rop.evidence).length ? <ul>{asStringList(rop.evidence).map((item) => <li key={item}>{formatMoneyText(item)}</li>)}</ul> : <span className="muted">Evidence не указано</span>}</div>
    </section>
  ) : null

  function toggleQualificationIssue(field: string) {
    setQualificationIssueFields((current) => current.includes(field) ? current.filter((item) => item !== field) : [...current, field])
  }

  const metricCards = isLead
    ? [
        {
          label: 'Клиент',
          value: client,
          hint: formatMoneyText(asString(leadState.need)) || undefined,
        },
        {
          label: 'Риск',
          value: riskRu,
          hint: riskType || (verdict ? verdictLabelRu(verdict) : undefined),
        },
        {
          label: 'Категория лида',
          value: qualification,
          hint: formatMoneyText(qualificationReason) || undefined,
        },
      ]
    : [
        {
          label: 'Сумма',
          value: amount,
          hint: formatMoneyText(asString(dealState.client)) || undefined,
        },
        {
          label: 'Стадия',
          value: stage,
          hint: formatMoneyText(riskType) || undefined,
        },
        {
          label: 'Риск',
          value: riskRu,
          hint: formatMoneyText(riskType) || undefined,
        },
      ]

  return (
    <>
      <section className="section" id="report">
        <div className="report-title-row">
          <div>
            <h2 style={{ margin: 0 }}>{title}</h2>
            {needsAttention ? <span className="attention-badge">Требует внимания РОПа</span> : null}
          </div>
          {bitrixUrl ? (
            <a className="btn secondary bitrix-link" href={bitrixUrl} target="_blank" rel="noreferrer">
              Открыть в Bitrix
            </a>
          ) : null}
        </div>

        {!isLead ? ropControlBlock : null}

        {isLead && hasQualificationAssessment ? (
          <section className="lead-qualification" aria-label="BANT и категория лида">
            <div className="qualification-summary-head">
              <div>
                <h3>BANT</h3>
                <p>Сначала факты по бюджету, ЛПР, потребности и сроку. Отсутствие данных не считается отказом.</p>
              </div>
              <span className={`assessment-status status-${asString(bant.overall_status, 'unknown')}`}>
                {bantStatus} · {confirmedBantCount} из 4
              </span>
            </div>
            <div className="bant-dashboard">
              {bantItems.map((item) => {
                const status = asString(item.value.status, 'unknown')
                const evidence = asStringList(item.value.evidence).map(formatMoneyText)
                const missing = asStringList(item.value.missing_facts).map(formatMoneyText)
                const question = formatMoneyText(
                  asString(item.value.next_question_or_action) ||
                    (status !== 'confirmed' ? nextQualificationQuestion : ''),
                )
                const explanation = formatMoneyText(
                  asString(item.value.summary) || evidence[0] || 'В сохранённом анализе нет отдельного объяснения.',
                )
                return (
                  <article className={`bant-card status-${status}`} key={item.key}>
                    <div className="bant-card-head">
                      <span className="bant-letter">{item.letter}</span>
                      <div>
                        <div className="bant-name">{asString(item.value.label) || item.fallbackLabel}</div>
                        <div className="bant-status">{assessmentLabel(status, BANT_STATUS_RU)}</div>
                      </div>
                    </div>
                    <p className="bant-summary">{explanation}</p>
                    <div className="bant-detail">
                      <div className="label">Подтверждающие факты</div>
                      {evidence.length ? <ul>{evidence.map((fact) => <li key={fact}>{fact}</li>)}</ul> : <div className="muted">Нет подтверждённых фактов</div>}
                    </div>
                    <div className="bant-detail">
                      <div className="label">Чего не хватает</div>
                      {missing.length ? <ul>{missing.map((fact) => <li key={fact}>{fact}</li>)}</ul> : <div className="muted">Ничего не указано</div>}
                    </div>
                    {question ? <div className="bant-next"><span>Вопрос / действие</span>{question}</div> : null}
                    {item.key === 'timeframe' ? <div className="timing-facts">
                      <div><span>Когда клиент принимает решение</span><strong>{formatMoneyText(asString(timeframeAssessment.decision_timing)) || 'не выяснено'}</strong></div>
                      <div><span>Когда нужно оборудование или запуск</span><strong>{formatMoneyText(asString(timeframeAssessment.need_or_launch_timing)) || 'не выяснено'}</strong></div>
                    </div> : null}
                  </article>
                )
              })}
            </div>

            <div className="lead-category-card">
              <div className="lead-category-value">
                <span>Категория лида</span>
                <strong>{qualification}</strong>
              </div>
              <div className="lead-category-content">
                <div>
                  <div className="label">Почему присвоена</div>
                  <p>{formatMoneyText(qualificationReason) || 'В старом анализе структурированное объяснение отсутствует.'}</p>
                </div>
                <div className="category-factor-grid">
                  <div><div className="label">BANT-факторы</div>{categoryBantFactors.length ? <ul>{categoryBantFactors.map((item) => <li key={item}>{item}</li>)}</ul> : <span className="muted">См. BANT выше</span>}</div>
                  <div><div className="label">Техника</div>{categoryTechnicalFactors.length ? <ul>{categoryTechnicalFactors.map((item) => <li key={item}>{item}</li>)}</ul> : <span>{solutionFitStatus}</span>}</div>
                  <div><div className="label">Бюджет нового оборудования</div>{categoryBudgetFactors.length ? <ul>{categoryBudgetFactors.map((item) => <li key={item}>{item}</li>)}</ul> : <span>{commercialFitStatus}</span>}</div>
                </div>
                {categoryMissingFacts.length ? <div><div className="label">Недостающие сведения</div><ul>{categoryMissingFacts.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
                <div className="category-next-step"><div className="label">Следующий шаг</div>{categoryNextStep || 'Проверить факты и определить следующий шаг.'}</div>
              </div>
            </div>

            {Object.keys(leadRoute).length ? (
              <div className={`lead-route-card route-${asString(leadRoute.status, 'unknown')}`}>
                <div><div className="label">Маршрут лида</div><strong>{routeStatus}</strong></div>
                <div><span>Сейчас: {currentRoute}</span><span>Рекомендуется: {recommendedRoute}</span></div>
                <p>{formatMoneyText(asString(leadRoute.reason))}</p>
                {leadRoute.controlled_return_required === true ? (
                  <div className={`controlled-return return-${asString(leadRoute.controlled_return_status, 'legacy')}`}>
                    <strong>Контролируемый возврат: {controlledReturnStatus || 'статус не разделён в старом анализе'}</strong>
                    {controlledReturnExistingDate ? <span>В CRM: {controlledReturnExistingDate}</span> : null}
                    {controlledReturnRecommendedDate ? <span>Рекомендуемая дата: {controlledReturnRecommendedDate}</span> : null}
                    {!controlledReturnExistingDate && !controlledReturnRecommendedDate ? <span>Дата не указана</span> : null}
                  </div>
                ) : null}
              </div>
            ) : null}

            {meta?.report_id ? (
              <div className="qualification-review-card">
                <div>
                  <div className="label">Проверка РОПом</div>
                  <strong>Оценка BANT и категория лида верны?</strong>
                  {latestQualificationReview ? (
                    <p className="muted">
                      Последняя проверка: {latestQualificationReview.is_correct ? 'оценка верна' : 'есть исправления'} · {asString(latestQualificationReview.created_at)}
                    </p>
                  ) : null}
                </div>
                <div className="qualification-review-actions">
                  <button className="btn secondary" onClick={() => {
                    setShowQualificationCorrection(false)
                    props.onQualificationReview({ is_correct: true })
                  }}>Да, верна</button>
                  <button className="btn ghost" onClick={() => setShowQualificationCorrection(true)}>Нет, исправить</button>
                </div>
                {showQualificationCorrection ? (
                  <div className="qualification-correction-form">
                    <div className="label">Где ошибка</div>
                    <div className="qualification-issue-grid">
                      {Object.entries(QUALIFICATION_ISSUE_LABELS).map(([field, label]) => (
                        <label key={field}>
                          <input type="checkbox" checked={qualificationIssueFields.includes(field)} onChange={() => toggleQualificationIssue(field)} />
                          {label}
                        </label>
                      ))}
                    </div>
                    {(['budget', 'authority', 'need', 'timeframe'] as const).filter((field) => qualificationIssueFields.includes(field)).map((field) => (
                      <div className="field qualification-correction-field" key={field}>
                        <label>Правильный статус {BANT_SHORT_LABELS[field]}</label>
                        <select value={correctedStatuses[field] || ''} onChange={(event) => setCorrectedStatuses((current) => ({ ...current, [field]: event.target.value }))}>
                          <option value="">Не указывать</option>
                          <option value="confirmed">Подтверждён</option>
                          <option value="not_confirmed">Не подтверждён</option>
                          <option value="negative">Подтверждён стоп-фактор</option>
                          <option value="unknown">Нет данных</option>
                        </select>
                      </div>
                    ))}
                    {qualificationIssueFields.includes('category') ? (
                      <div className="field qualification-correction-field">
                        <label>Правильная категория лида</label>
                        <select value={correctedCategory} onChange={(event) => setCorrectedCategory(event.target.value)}>
                          <option value="">Не указывать</option>
                          {['A', 'B', 'C', 'D', 'E', 'unknown'].map((value) => <option value={value} key={value}>{value === 'unknown' ? 'Unknown' : value}</option>)}
                        </select>
                      </div>
                    ) : null}
                    <div className="field qualification-correction-field">
                      <label>Комментарий</label>
                      <textarea value={qualificationComment} maxLength={800} onChange={(event) => setQualificationComment(event.target.value)} placeholder="Как должно быть и на каком факте это основано" />
                    </div>
                    <button className="btn" disabled={!qualificationIssueFields.length} onClick={() => {
                      props.onQualificationReview({
                        is_correct: false,
                        issue_fields: qualificationIssueFields,
                        corrected_statuses: correctedStatuses,
                        corrected_category: correctedCategory || null,
                        comment: qualificationComment.trim() || null,
                      })
                      setShowQualificationCorrection(false)
                    }}>Сохранить исправление</button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}
        {isLead ? ropControlBlock : null}
        {!isLead && hasQualificationAssessment ? (
          <section className="qualification-summary" aria-label="Квалификация и применимость">
            <div className="qualification-summary-head">
              <div>
                <h3>Квалификация и применимость</h3>
                <p>Основание категории и следующий факт, который нужно подтвердить.</p>
              </div>
            </div>
            {hasQualificationAssessment ? (
              <>
                <div className="cards qualification-cards">
                  <div className="card">
                    <div className="label">BANT</div>
                    <div className="value">{bantStatus}</div>
                    <div className="hint">Бюджет, ЛПР, потребность и срок</div>
                  </div>
                  <div className="card">
                    <div className="label">Техническая применимость</div>
                    <div className="value">{solutionFitStatus}</div>
                    {asString(solutionFit.reason_code) && asString(solutionFit.reason_code) !== 'unknown' ? (
                      <div className="hint">{verdictLabelRu(asString(solutionFit.reason_code))}</div>
                    ) : null}
                  </div>
                  <div className="card">
                    <div className="label">Бюджет нового оборудования</div>
                    <div className="value">{commercialFitStatus}</div>
                    <div className="hint">
                      {confirmedBudget && confirmedBudget !== '—' ? `Подтверждён: ${confirmedBudget}. ` : ''}
                      Порог: {minimumBudget}
                    </div>
                  </div>
                  <div className="card">
                    <div className="label">{isLead ? 'Категория и вердикт' : 'Режим и приоритет'}</div>
                    <div className="value">{isLead ? qualification : asString(dealMode.mode, '—')}</div>
                    <div className="hint">
                      {isLead
                        ? verdict
                          ? verdictLabelRu(verdict)
                          : 'Вердикт не указан'
                        : asString(priority.priority, 'Приоритет не указан')}
                    </div>
                  </div>
                </div>
                {nextQualificationQuestion ? (
                  <div className="qualification-question">
                    <div className="label">Один вопрос клиенту</div>
                    <div>{nextQualificationQuestion}</div>
                  </div>
                ) : null}
              </>
            ) : null}
          </section>
        ) : null}
        {internalChecks.length ? (
          <div className="internal-checks">
            <div className="label">Внутренняя проверка РОПа / техспециалиста</div>
            <ul className="facts">
              {internalChecks.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section className="section manager-task-section">
        <div className="section-head">
          <div>
            <h2>Задача менеджеру</h2>
            <p>То, что РОП может отправить без аналитики и лишнего контекста.</p>
          </div>
          <button className="btn" onClick={props.onCopy} disabled={!hasAnalysis}>
            Скопировать
          </button>
        </div>
        {hasAnalysis ? (
          <div className="task">
            <div className="manager-brief">
              <div className="manager-brief-block">
                <div className="label">Задача</div>
                <p>{managerBrief.task}</p>
              </div>
              <div className="manager-brief-block">
                <div className="label">Цель</div>
                <p>{managerBrief.goal}</p>
              </div>
              <div className="manager-brief-block client-message">
                <div className="label">Текст клиенту{managerBrief.clientChannel ? ` · ${managerBrief.clientChannel}` : ''}</div>
                <p>{managerBrief.clientText}</p>
              </div>
              <div className="manager-brief-block">
                <div className="label">Что зафиксировать в CRM</div>
                <p>{managerBrief.crm}</p>
              </div>
            </div>
          </div>
        ) : (
          <div className="empty-task">
            После анализа здесь появится готовое поручение менеджеру: задача, цель, текст клиенту и факт для CRM.
          </div>
        )}
      </section>

      {meta?.report_id ? (
        <section className="section">
          <h2>Решение РОПа</h2>
          {meta.candidate_review && asString(meta.candidate_review.state) !== 'active' ? (
            <div className="review-status">
              Проверено РОПом: {asString(meta.candidate_review.state) === 'snoozed' ? 'вернётся в контроль по дате' : 'скрыто из основной очереди до изменений'}.
            </div>
          ) : null}
          <div className="actions">
            {DECISIONS.map((item) => (
              <button key={item} onClick={() => props.onDecision(item)}>
                {item}
              </button>
            ))}
          </div>
          {!!props.decisions?.length && (
            <p className="muted" style={{ marginTop: 12 }}>
              Последнее: {asString(props.decisions[0].decision)} ({asString(props.decisions[0].created_at)})
            </p>
          )}
        </section>
      ) : null}

      <section className="section context-section">
        <h2>Контекст решения</h2>
        <div className="cards cards-metrics">
          {metricCards.map((card) => (
            <div className="card" key={card.label}>
              <div className="label">{card.label}</div>
              <div className="value">{card.value}</div>
              {card.hint ? <div className="hint">{card.hint}</div> : null}
            </div>
          ))}
        </div>
      </section>

      <section className="section two">
        <div>
          <h2>Факты</h2>
          <ul className="facts good">
            {facts.map((item) => (
              <li key={item}>{item}</li>
            ))}
            {!facts.length && <li className="muted">Пока нет фактов</li>}
          </ul>
        </div>
        <div>
          <h2>Что неизвестно</h2>
          <ul className="facts warn">
            {unknowns.map((item) => (
              <li key={item}>{item}</li>
            ))}
            {!unknowns.length && <li className="muted">Пробелы не выделены</li>}
          </ul>
        </div>
      </section>

      {closureReasons.length ? (
        <section className="section">
          <h2>Почему закрытие спорно</h2>
          <ul className="facts warn">
            {closureReasons.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="section">
        <h2>Как принять решение</h2>
        <ul className="facts bad">
          {recommendations.map((item) => (
            <li key={item}>{item}</li>
          ))}
          {!recommendations.length && <li className="muted">Нет рекомендации</li>}
        </ul>
      </section>

      {meta?.report_id ? (
        <section className="section">
          <h2>Исход после рекомендации</h2>
          <div className="actions">
            {OUTCOMES.map((item) => (
              <button key={item} onClick={() => props.onOutcome(item)}>
                {item}
              </button>
            ))}
          </div>
          {!!props.outcomes?.length && (
            <p className="muted" style={{ marginTop: 12 }}>
              Последний исход: {asString(props.outcomes[0].outcome_type)} ({asString(props.outcomes[0].checked_at)})
            </p>
          )}
        </section>
      ) : null}

      <section className="section">
        <h2>Полный markdown-отчёт</h2>
        <p>Большой аудитный текст. Открывается только по запросу.</p>
        <button className="btn secondary" onClick={props.onToggleMarkdown} disabled={!meta?.report_id}>
          {props.showMarkdown ? 'Скрыть полный отчёт' : 'Показать полный отчёт'}
        </button>
        {props.showMarkdown && props.markdown && <div className="markdown">{formatMoneyText(props.markdown)}</div>}
      </section>
    </>
  )
}

function compactEvidenceIds(value: unknown): string[] {
  const result: string[] = []
  const visit = (item: unknown) => {
    const record = asRecord(item)
    if (Array.isArray(record.evidence_ids)) result.push(...asStringList(record.evidence_ids))
    if (Array.isArray(item)) item.forEach(visit)
    else if (item && typeof item === 'object') Object.values(record).forEach(visit)
  }
  visit(value)
  return [...new Set(result)]
}

function CompactReviewPanel(props: {
  meta: ReportPanelsProps['meta']
  fullAnalysis: Record<string, unknown> | null
  comparison: boolean
}) {
  const entityType = props.meta?.entity_type === 'lead' ? 'lead' : props.meta?.entity_type === 'deal' ? 'deal' : null
  const entityId = props.meta?.entity_id || ''
  const [review, setReview] = useState<CompactReview | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null)
  const [feedbackReason, setFeedbackReason] = useState('')
  const [feedbackComment, setFeedbackComment] = useState('')

  const load = useCallback(async (runId?: string) => {
    if (!entityType || !entityId) return
    setLoading(true)
    setError(null)
    try {
      setReview(await fetchCompactReview(entityType, entityId, runId))
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError))
    } finally {
      setLoading(false)
    }
  }, [entityId, entityType])

  useEffect(() => {
    setReview(null)
    setEvidence(null)
    void load()
  }, [load])

  const run = review?.selected_run || null
  const analysis = asRecord(run?.analysis)
  const compactReview = asRecord(entityType === 'lead' ? analysis.lead_review : analysis.deal_review)
  const compactUi = asRecord(analysis._ui)
  const action = asRecord(analysis.rop_action)
  const coverage = asRecord(run?.evidence_coverage)
  const isFallback = run?.fallback_class === 'full_fallback_recommended' || run?.status === 'error'
  const compactStatus = isFallback
    ? 'Нужен полный анализ'
    : run?.status === 'completed'
      ? 'Compact safe'
      : 'Требуется ручная проверка'

  const start = async () => {
    if (!entityType || !entityId) return
    setRunning(true)
    setError(null)
    try {
      const job = await startCompactRun(entityType, entityId)
      let current = job
      while (current.status === 'queued' || current.status === 'running') {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        current = await fetchCompactJob(job.job_id)
      }
      if (current.status === 'error') throw new Error(current.error || 'Compact-анализ не завершён')
      await load(current.run_id || undefined)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError))
    } finally {
      setRunning(false)
    }
  }

  const showEvidence = async (evidenceId: string) => {
    if (!entityType || !entityId) return
    try {
      setEvidence(await fetchCompactEvidence(entityType, entityId, evidenceId))
    } catch (nextError) {
      setEvidence({ error: nextError instanceof Error ? nextError.message : String(nextError) })
    }
  }

  const submitFeedback = async (result: 'correct' | 'partly_correct' | 'error') => {
    if (!entityType || !entityId || !run) return
    if (result !== 'correct' && !feedbackComment.trim() && !feedbackReason) {
      setError('Для этой оценки укажите причину или короткий комментарий.')
      return
    }
    try {
      await saveCompactFeedback(entityType, entityId, run.id, result, feedbackReason, feedbackComment)
      await load(run.id)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError))
    }
  }

  if (!props.meta || !entityType) return <section className="section">Выберите лид или сделку.</section>
  if (loading && !review) return <section className="section muted">Загрузка Compact-данных…</section>

  if (props.comparison) {
    const fullLead = asRecord(props.fullAnalysis?.lead_state)
    const fullDeal = asRecord(props.fullAnalysis?.deal_state)
    const fullRisk = asRecord(props.fullAnalysis?.main_risk)
    const fullLoss = asRecord(props.fullAnalysis?.loss_diagnosis)
    const rows = [
      ['Qualification', entityType === 'lead' ? asString(fullLead.qualification, '—') : asString(fullDeal.stage, '—'), entityType === 'lead' ? asString(compactReview.qualification, '—') : asString(compactReview.decision, '—')],
      ['Final verdict', asString(fullLoss.final_verdict, '—'), asString(compactReview.final_verdict, asString(compactReview.closure_status, '—'))],
      ['Attention required', asString(props.meta.attention_reason, '—'), analysis.attention_required === true ? 'Да' : analysis.attention_required === false ? 'Нет' : '—'],
      ['Severity', asString(fullRisk.risk_level, '—'), asString(analysis.severity, '—')],
      ['Playbook', '—', asString(compactReview.action_playbook, '—')],
      ['Основной риск', asString(fullRisk.description, '—'), asString(analysis.reason, '—')],
      ['Следующий шаг', asString(asRecord(props.fullAnalysis?.rop_manager_message_block).check_for_rop, '—'), asString(action.message_to_manager, '—')],
      ['Deadline', '—', asString(action.deadline, '—')],
      ['Expected CRM fact', asString(asRecord(props.fullAnalysis?.rop_manager_message_block).expected_crm_fact, '—'), asString(action.expected_crm_fact, '—')],
      ['Evidence', compactEvidenceIds(props.fullAnalysis).join(', ') || '—', compactEvidenceIds(analysis).join(', ') || '—'],
      ['Стоимость', '—', run?.cost_rub == null ? '—' : `${run.cost_rub} ₽`],
    ]
    return (
      <section className="section">
        <h2>Сравнение анализов</h2>
        {!run ? <p className="muted">Compact-анализ ещё не выполнен.</p> : (
          <div className="comparison-table">
            {rows.map(([label, full, compact]) => <div className={full === compact ? 'same' : 'different'} key={label}><b>{label}</b><span>{full}</span><span>{compact}</span></div>)}
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="section compact-panel">
      <div className="section-head">
        <div><h2>Compact beta</h2><p>Тестовый анализ. Не заменяет основной и не изменяет Bitrix.</p></div>
        <button className="btn" onClick={() => void start()} disabled={running || Boolean(review?.preflight_error)}>
          {running ? 'Выполняется…' : 'Запустить Compact-анализ'}
        </button>
      </div>
      {review?.preflight_error ? <div className="alert error">{review.preflight_error}</div> : null}
      {error ? <div className="alert error">{error}</div> : null}
      {!run ? <p className="muted">Compact-анализ ещё не выполнен.</p> : <>
        <div className={`compact-status ${isFallback ? 'fallback' : run.status === 'completed' ? 'safe' : 'review'}`}>
          <b>{compactStatus}</b><span>{asString(analysis.reason) || asString(coverage.status) || 'Статус пока не определён'}</span>
        </div>
        <div className="cards cards-metrics">
          <div className="card"><div className="label">Время запуска</div><div className="value">{run.started_at}</div></div>
          <div className="card"><div className="label">Актуальность</div><div className="value">{run.is_current ? 'Актуален' : 'Данные изменились'}</div></div>
          <div className="card"><div className="label">Snapshot hash</div><div className="value">{run.snapshot_hash.slice(0, 12)}</div></div>
        </div>
        {run.is_current ? <p className="muted">После прошлого Compact-анализа новых данных не обнаружено.</p> : null}
        <div className="reason-box"><div className="label">Что произошло</div><div className="reason-text">{asString(analysis.reason, '—')}</div></div>
        <div className="action-banner"><div className="label">Действие менеджеру</div><div className="value">{asString(action.message_to_manager, '—')}</div></div>
        <div className="compact-details">
          <div><b>Raw playbook</b><span>{asString(compactUi.raw_playbook, '—')}</span></div>
          <div><b>Playbook</b><span>{asString(compactReview.action_playbook, '—')}</span></div>
          <div><b>Причина нормализации</b><span>{asString(compactUi.normalization_reason, '—')}</span></div>
          <div><b>Квалификация / решение</b><span>{asString(compactReview.qualification, asString(compactReview.decision, '—'))}</span></div>
          <div><b>Что проверить РОПу</b><span>{asString(action.check, '—')}</span></div>
          <div><b>Deadline</b><span>{asString(action.deadline, '—')}</span></div>
          <div><b>Expected CRM fact</b><span>{asString(action.expected_crm_fact, '—')}</span></div>
          <div><b>Evidence coverage</b><span>{asString(coverage.status, '—')} · {asString(coverage.coverage_percent, '—')}%</span></div>
          <div><b>Fallback class</b><span>{asString(run.fallback_class, '—')}</span></div>
        </div>
        <div className="evidence-list"><b>Evidence</b>{compactEvidenceIds(analysis).map((id) => <button key={id} onClick={() => void showEvidence(id)}>{id}</button>)}{!compactEvidenceIds(analysis).length ? <span>Исходный evidence не найден в переданном контексте</span> : null}</div>
        {evidence ? <div className="evidence-drawer"><button className="close" onClick={() => setEvidence(null)}>×</button><b>{asString(evidence.evidence_id, 'Evidence')}</b><p>{asString(evidence.source_type)} · {asString(evidence.namespace)}</p><pre>{asString(evidence.fragment, asString(evidence.error))}</pre></div> : null}
        <details><summary>Технические данные анализа</summary><p>Модель: {run.model || '—'} · tokens: {asString(asRecord(run.usage).total_tokens, '—')} · стоимость: {run.cost_rub ?? '—'} ₽</p></details>
        <div className="feedback"><h3>Оценка результата</h3><select value={feedbackReason} onChange={(event) => setFeedbackReason(event.target.value)}><option value="">Выберите причину (необязательно для «Верно»)</option><option>ложная тревога РОПу</option><option>пропущен риск</option><option>неверный playbook</option><option>неверная qualification</option><option>неверный deadline</option><option>выдуманный факт</option><option>неверный fallback</option><option>evidence не подтверждает вывод</option><option>поручение менеджеру непрактично</option><option>другое</option></select><textarea value={feedbackComment} onChange={(event) => setFeedbackComment(event.target.value)} placeholder="Короткий комментарий" maxLength={800} /><div className="actions"><button onClick={() => void submitFeedback('correct')}>Верно</button><button onClick={() => void submitFeedback('partly_correct')}>Частично верно</button><button onClick={() => void submitFeedback('error')}>Ошибка</button></div>{run.feedback ? <p className="muted">Оценка сохранена: {asString(run.feedback.feedback_result)}</p> : null}</div>
        {!!review?.runs.length && <div className="compact-history"><h3>История Compact-анализов</h3>{review.runs.map((item) => <button className={item.id === run.id ? 'active' : ''} key={item.id} onClick={() => void load(item.id)}>{item.started_at.slice(0, 16).replace('T', ' ')} · {asString(asRecord(entityType === 'lead' ? asRecord(item.analysis).lead_review : asRecord(item.analysis).deal_review).action_playbook, '—')} · {item.fallback_class || item.status}</button>)}</div>}
      </>}
    </section>
  )
}
