import { useEffect, useMemo, useState } from 'react'
import './index.css'
import {
  asRecord,
  asString,
  asStringList,
  unwrapAnalysis,
  type AnalyzeOptions,
  type Candidate,
  type CandidateFilter,
  type CandidatesResponse,
  type CrmPipeline,
  type JobState,
  type UiReportDetail,
  type UiReportListItem,
  fetchCandidateFilter,
  fetchJob,
  fetchPipelines,
  fetchReport,
  fetchReportMarkdown,
  fetchReports,
  saveDecision,
  saveOutcome,
  searchCandidates,
  startAnalyze,
} from './api'

type Tab = 'dashboard' | 'manual' | 'history'

type ManualEntityType = 'lead' | 'deal' | 'auto'

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
  unknown: 'неясно',
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
      asString(rop.expected_crm_update) || asStringList(manager.manager_checklist).join('\n') || '—',
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
  const closed = asRecord(analysis?.closed_deal_review)
  const items = [
    ...asStringList(price.what_is_unclear),
    ...asStringList(payment.missing_confirmation),
    ...asStringList(closed.why_closed_questionable),
  ]
  if (!items.length) {
    const risk = asRecord(analysis?.main_risk)
    if (risk.description) items.push(asString(risk.description))
  }
  return items.slice(0, 8).map(formatMoneyText)
}

function ropRecommendations(analysis: Record<string, unknown> | null | undefined): string[] {
  const rop = asRecord(analysis?.rop_manager_message_block)
  const closed = asRecord(analysis?.closed_deal_review)
  const mode = asRecord(analysis?.deal_mode)
  const items = [
    asString(rop.check_for_rop),
    asString(rop.why_it_matters),
    asString(closed.recommended_pipeline_action),
    asString(mode.rop_focus),
  ].filter(Boolean)
  return items.slice(0, 6).map(formatMoneyText)
}

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const [toastMessage, setToastMessage] = useState<string | null>(null)

  const [createdDays, setCreatedDays] = useState(15)
  const [modifiedDays, setModifiedDays] = useState(15)
  const [entityFilter, setEntityFilter] = useState<'lead' | 'deal'>('lead')
  const [priorityFilter, setPriorityFilter] = useState<string>('')
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
    force_llm: true,
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
      }
    }
    return null
  }, [tab, selectedReport, selectedCandidate, activeAnalysis, job, selectedResultIndex, pendingAnalyzeMeta])

  function applyFilterState(filter: CandidateFilter) {
    setEntityFilter(filter.entity_type === 'deal' ? 'deal' : 'lead')
    setCreatedDays(Number(filter.created_days) || 15)
    setModifiedDays(Number(filter.modified_days) || 15)
    setPriorityFilter(filter.priority || '')
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
    return `${date} · ${risk} · ${action}`
  }

  async function loadCandidates(overrides?: {
    entity_type?: 'lead' | 'deal'
    created_days?: number
    modified_days?: number
    priority?: string
    pipeline_ids?: string[]
    stage_ids?: string[]
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
        pipeline_ids: overrides?.pipeline_ids ?? pipelineIds,
        stage_ids: overrides?.stage_ids ?? stageIds,
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
        const [pipelines, savedFilter, reports] = await Promise.all([
          fetchPipelines(),
          fetchCandidateFilter(),
          fetchReports(50),
        ])
        if (cancelled) return
        setDealPipelines(pipelines.deal_pipelines || [])
        setLeadPipeline(pipelines.lead_pipeline || null)
        setHistory(reports.items)

        const filter = savedFilter.filter
        applyFilterState(filter)
        setFiltersReady(true)

        // Ищем только если в сохранённом фильтре уже выбраны этапы (и воронки для сделок).
        const data = await searchCandidates({
          entity_type: filter.entity_type === 'deal' ? 'deal' : 'lead',
          created_days: Number(filter.created_days) || 15,
          modified_days: Number(filter.modified_days) || 15,
          limit: Number(filter.limit) || 20,
          priority: filter.priority || null,
          pipeline_ids: filter.pipeline_ids || [],
          stage_ids: filter.stage_ids || [],
          save: false,
        })
        if (cancelled) return
        setCandidatesData(data)
        setSelectedCandidate(data.candidates[0] || null)
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
      const started = await startAnalyze({ ...options, ids, entity_type: entityType })
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

  const summary = candidatesData?.summary
  const manualInput = parseManualInput(manualIds, options.entity_type)
  const managerBrief = getManagerBrief(activeAnalysis)
  const copyText = buildManagerCopy(managerBrief)
  const facts = activeAnalysis ? evidenceList(activeAnalysis) : selectedCandidate?.reasons || []
  const unknowns = activeAnalysis ? unknownsList(activeAnalysis) : ['Полный разбор появится после LLM-анализа']
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
                  </span>
                  <small>
                    {formatMoneyText(item.status)}
                    {item.amount ? ` · ${formatMoney(item.amount)}` : ''}
                    <br />
                    {formatMoneyText(item.attention_reason)}
                  </small>
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
                  <div className="label">Уже разобраны</div>
                  <div className="value">{summary?.already_analyzed ?? '—'}</div>
                </div>
              </div>
            </section>

            <ReportPanels
              meta={activeMeta}
              analysis={activeAnalysis}
              facts={facts}
              unknowns={unknowns}
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
            <p>Для срочной проверки конкретного лида или сделки.</p>
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
              analysis={activeAnalysis}
              facts={facts}
              unknowns={unknowns}
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
                </button>
              ))}
              {!history.length && !historyError && <p className="muted">Пока нет сохранённых отчётов.</p>}
            </div>
          </aside>
          <main>
            <ReportPanels
              meta={activeMeta}
              analysis={activeAnalysis}
              facts={facts}
              unknowns={unknowns}
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

function ReportPanels(props: {
  meta: {
    entity_type: string
    entity_id: string
    report_id: number | null
    risk_level?: string | null
    attention_reason?: string | null
    recommended_action?: string | null
    bitrix_url?: string | null
  } | null
  analysis: Record<string, unknown> | null
  facts: string[]
  unknowns: string[]
  recommendations: string[]
  managerBrief: ManagerBrief
  showMarkdown: boolean
  markdown: string | null
  onCopy: () => void
  onToggleMarkdown: () => void
  onDecision: (value: string) => void
  onOutcome: (value: string) => void
  decisions?: Array<Record<string, unknown>>
  outcomes?: Array<Record<string, unknown>>
}) {
  const { meta, analysis, facts, unknowns, recommendations, managerBrief } = props
  const isLead = meta?.entity_type === 'lead'
  const dealState = asRecord(analysis?.deal_state)
  const leadState = asRecord(analysis?.lead_state)
  const mainRisk = asRecord(analysis?.main_risk)
  const loss = asRecord(analysis?.loss_diagnosis)
  const rop = asRecord(analysis?.rop_manager_message_block)

  const riskLevel = asString(mainRisk.risk_level) || asString(meta?.risk_level) || ''
  const riskRu = riskLabelRu(riskLevel)
  const riskType = asString(mainRisk.risk_type)
  const verdict = asString(loss.final_verdict)
  const client = asString(leadState.client) || asString(dealState.client) || '—'
  const qualification = asString(leadState.qualification) || '—'
  const qualificationReason = asString(leadState.qualification_reason)
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
          label: 'Квалификация',
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

        <div className="reason-box">
          <div className="label">Причина внимания</div>
          <div className="reason-text">{attention}</div>
        </div>

        <div className="action-banner">
          <div className="label">Что сделать</div>
          <div className="value">{nextAction}</div>
        </div>
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
          <h2>Что неизвестно / не зафиксировано</h2>
          <ul className="facts warn">
            {unknowns.map((item) => (
              <li key={item}>{item}</li>
            ))}
            {!unknowns.length && <li className="muted">Пробелы не выделены</li>}
          </ul>
        </div>
      </section>

      <section className="section">
        <h2>Рекомендация РОПу</h2>
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
