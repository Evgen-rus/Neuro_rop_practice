import { useEffect, useMemo, useState } from 'react'
import './index.css'
import {
  asRecord,
  asString,
  asStringList,
  unwrapAnalysis,
  type AnalyzeOptions,
  type Candidate,
  type CandidatesResponse,
  type JobState,
  type UiReportDetail,
  type UiReportListItem,
  fetchCandidates,
  fetchJob,
  fetchReport,
  fetchReportMarkdown,
  fetchReports,
  saveDecision,
  saveOutcome,
  startAnalyze,
} from './api'

type Tab = 'dashboard' | 'manual' | 'history'

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

function toast(message: string, setter: (value: string | null) => void) {
  setter(message)
  window.setTimeout(() => setter(null), 2200)
}

function buildManagerCopy(analysis: Record<string, unknown> | null | undefined): string {
  const rop = asRecord(analysis?.rop_manager_message_block)
  const manager = asRecord(analysis?.manager_action_block)
  const primary = asRecord(manager.primary_text)
  const task = asString(rop.message_to_manager) || asString(rop.check_for_rop) || '—'
  const goal = asString(rop.success_condition) || asString(rop.expected_crm_update) || '—'
  const clientText =
    asString(primary.email_or_messenger) ||
    asString(primary.call_script) ||
    asString(primary.text) ||
    'Текст клиенту не сформирован (возможно, сначала нужна проверка РОПа).'
  const crm =
    asString(rop.expected_crm_update) ||
    asStringList(manager.manager_checklist).join('\n') ||
    '—'
  return `Задача:
${task}

Цель:
${goal}

Текст клиенту:
${clientText}

Что зафиксировать в CRM:
${crm}`
}

function evidenceList(analysis: Record<string, unknown> | null | undefined): string[] {
  const rop = asRecord(analysis?.rop_manager_message_block)
  const money = asRecord(analysis?.money_path_diagnosis)
  const loss = asRecord(analysis?.loss_diagnosis)
  const fromRop = asStringList(rop.evidence)
  const fromMoney = asStringList(money.evidence)
  const fromLoss = asStringList(loss.evidence)
  return [...fromRop, ...fromMoney, ...fromLoss].slice(0, 8)
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
  return items.slice(0, 8)
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
  return items.slice(0, 6)
}

export default function App() {
  const [tab, setTab] = useState<Tab>('dashboard')
  const [toastMessage, setToastMessage] = useState<string | null>(null)

  const [days, setDays] = useState(15)
  const [entityFilter, setEntityFilter] = useState<'all' | 'lead' | 'deal'>('all')
  const [priorityFilter, setPriorityFilter] = useState<string>('')
  const [candidatesData, setCandidatesData] = useState<CandidatesResponse | null>(null)
  const [candidatesLoading, setCandidatesLoading] = useState(false)
  const [candidatesError, setCandidatesError] = useState<string | null>(null)
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null)

  const [manualIds, setManualIds] = useState('')
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
      }
    }
    if (selectedCandidate && !activeAnalysis) {
      return {
        entity_type: selectedCandidate.entity_type,
        entity_id: selectedCandidate.entity_id,
        report_id: null as number | null,
        risk_level: selectedCandidate.priority,
        attention_reason: selectedCandidate.attention_reason,
        recommended_action: 'Запустить анализ и получить поручение менеджеру',
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
      }
    }
    return null
  }, [tab, selectedReport, selectedCandidate, activeAnalysis, job, selectedResultIndex])

  async function loadCandidates() {
    setCandidatesLoading(true)
    setCandidatesError(null)
    try {
      const data = await fetchCandidates({
        entity_type: entityFilter,
        days,
        limit: 20,
        priority: priorityFilter || undefined,
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

  async function loadHistory() {
    setHistoryError(null)
    try {
      const data = await fetchReports(50)
      setHistory(data.items)
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : String(error))
    }
  }

  useEffect(() => {
    void loadCandidates()
    void loadHistory()
  }, [])

  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'error') return
    const timer = window.setInterval(() => {
      void fetchJob(job.job_id)
        .then((next) => {
          setJob(next)
          if (next.status === 'done') {
            void loadHistory()
            toast('Анализ завершён', setToastMessage)
          }
        })
        .catch((error) => setJobError(error instanceof Error ? error.message : String(error)))
    }, 2000)
    return () => window.clearInterval(timer)
  }, [job?.job_id, job?.status])

  async function runAnalyze(ids: string, entityType: AnalyzeOptions['entity_type'] = options.entity_type) {
    setJobError(null)
    setSelectedResultIndex(0)
    setShowMarkdown(false)
    setMarkdown(null)
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
  const copyText = buildManagerCopy(activeAnalysis)
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
          <button className={tab === 'history' ? 'active' : ''} onClick={() => { setTab('history'); void loadHistory() }}>
            История
          </button>
        </div>
        <div className="pill">локально · read-only Bitrix</div>
      </div>

      <section className="hero">
        <span className="tag">не чат-бот</span>
        <span className="tag">не CRM</span>
        <span className="tag">контроль сделок и лидов</span>
        <h1>Сегодня есть сделки и лиды, где РОПу нужно вмешаться или поставить точную задачу менеджеру</h1>
        <p>
          Система собирает CRM, звонки и транскрибации, затем даёт разбор: почему сущность требует внимания
          и что сделать дальше. В Bitrix ничего не пишем.
        </p>
      </section>

      {tab === 'dashboard' && (
        <div className="grid">
          <aside className="panel">
            <h3>Сегодня требуют внимания</h3>
            <p>Топ-20 кандидатов по Bitrix-фильтру и scoring без LLM.</p>
            <div className="filters" style={{ gridTemplateColumns: '1fr 1fr' }}>
              <div className="field">
                <label>Дней</label>
                <input
                  type="number"
                  min={0}
                  value={days}
                  onChange={(e) => setDays(Math.max(0, Number(e.target.value) || 0))}
                />
              </div>
              <div className="field">
                <label>Тип</label>
                <select value={entityFilter} onChange={(e) => setEntityFilter(e.target.value as typeof entityFilter)}>
                  <option value="all">Все</option>
                  <option value="deal">Сделки</option>
                  <option value="lead">Лиды</option>
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
                <label>&nbsp;</label>
                <button className="btn secondary" onClick={() => void loadCandidates()} disabled={candidatesLoading}>
                  {candidatesLoading ? 'Загрузка…' : 'Обновить'}
                </button>
              </div>
            </div>
            {candidatesError && (
              <div className="alert error">
                <strong>Не удалось загрузить кандидатов:</strong> {candidatesError}
              </div>
            )}
            {(candidatesData?.candidates || []).map((item) => (
              <button
                key={`${item.entity_type}-${item.entity_id}`}
                className={`deal ${selectedCandidate?.entity_id === item.entity_id && selectedCandidate.entity_type === item.entity_type ? 'active' : ''}`}
                onClick={() => setSelectedCandidate(item)}
              >
                <strong>
                  {item.entity_type === 'deal' ? 'Сделка' : 'Лид'} {item.entity_id} · {item.client_name || item.title}
                </strong>
                <small>
                  {item.status}
                  {item.amount ? ` · ${item.amount}` : ''}
                  <br />
                  {item.attention_reason}
                </small>
                <span className={`priority ${item.priority}`}>{item.priority}</span>
                {item.analyzed ? <span className="priority low">уже есть анализ</span> : null}
              </button>
            ))}
            {!candidatesLoading && !candidatesData?.candidates?.length && !candidatesError && (
              <p className="muted">Кандидатов за выбранный период не найдено.</p>
            )}
            <button
              className="btn"
              style={{ marginTop: 10 }}
              disabled={!selectedCandidate}
              onClick={() => {
                if (!selectedCandidate) return
                void runAnalyze(selectedCandidate.entity_id, selectedCandidate.entity_type)
              }}
            >
              Запустить анализ выбранного
            </button>
          </aside>

          <main>
            <section className="section">
              <div className="alert">
                <strong>Первые 5 минут:</strong> сразу список контроля, без настройки pipeline. Полный markdown-отчёт
                открывается только по запросу.
              </div>
              <div className="cards">
                <div className="card">
                  <div className="label">В топе</div>
                  <div className="value">{summary?.returned ?? '—'}</div>
                </div>
                <div className="card">
                  <div className="label">Высокий приоритет</div>
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
              copyText={copyText}
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
            <p>ID через запятую или столбиком. Опции как в CLI.</p>
            <div className="field">
              <label>ID лидов/сделок</label>
              <textarea
                value={manualIds}
                onChange={(e) => setManualIds(e.target.value)}
                placeholder={'18457, 18533\nили столбиком'}
              />
            </div>
            <div className="filters" style={{ gridTemplateColumns: '1fr 1fr' }}>
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
            <button
              className="btn"
              onClick={() => void runAnalyze(manualIds, options.entity_type)}
              disabled={!manualIds.trim() || (job?.status === 'running' || job?.status === 'queued')}
            >
              Запустить анализ
            </button>
            {jobError && (
              <div className="alert error" style={{ marginTop: 12 }}>
                <strong>Ошибка:</strong> {jobError}
              </div>
            )}
            {job && (
              <div style={{ marginTop: 16 }}>
                <h3>Прогресс</h3>
                <p className="muted">
                  job {job.job_id} · {job.status}
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
              copyText={copyText}
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
            <button className="btn secondary" onClick={() => void loadHistory()}>
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
                  <small>
                    {item.created_at}
                    <br />
                    {item.attention_reason || item.recommended_action || '—'}
                  </small>
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
              copyText={copyText}
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
  } | null
  analysis: Record<string, unknown> | null
  facts: string[]
  unknowns: string[]
  recommendations: string[]
  copyText: string
  showMarkdown: boolean
  markdown: string | null
  onCopy: () => void
  onToggleMarkdown: () => void
  onDecision: (value: string) => void
  onOutcome: (value: string) => void
  decisions?: Array<Record<string, unknown>>
  outcomes?: Array<Record<string, unknown>>
}) {
  const { meta, analysis, facts, unknowns, recommendations, copyText } = props
  const dealState = asRecord(analysis?.deal_state)
  const leadState = asRecord(analysis?.lead_state)
  const mainRisk = asRecord(analysis?.main_risk)
  const rop = asRecord(analysis?.rop_manager_message_block)
  const amount = asString(dealState.amount) || asString(leadState.amount) || '—'
  const status =
    asString(dealState.stage) ||
    asString(leadState.status) ||
    asString(mainRisk.risk_level) ||
    asString(meta?.risk_level) ||
    '—'
  const attention =
    asString(meta?.attention_reason) ||
    asString(mainRisk.description) ||
    asString(rop.why_it_matters) ||
    asString(leadState.summary) ||
    asString(dealState.summary) ||
    '—'
  const nextAction =
    asString(meta?.recommended_action) ||
    asString(rop.check_for_rop) ||
    asString(rop.message_to_manager) ||
    '—'
  const title = meta
    ? `${meta.entity_type === 'deal' ? 'Сделка' : meta.entity_type === 'lead' ? 'Лид' : meta.entity_type} ${meta.entity_id}`
    : 'Выберите кандидата или запустите анализ'

  return (
    <>
      <section className="section" id="report">
        <h2>{title}</h2>
        <div className="cards">
          <div className="card">
            <div className="label">Потенциал / сумма</div>
            <div className="value">{amount}</div>
          </div>
          <div className="card">
            <div className="label">Статус / риск</div>
            <div className="value">{status}</div>
          </div>
          <div className="card">
            <div className="label">Причина внимания</div>
            <div className="value">{attention}</div>
          </div>
          <div className="card">
            <div className="label">Что сделать</div>
            <div className="value">{nextAction}</div>
          </div>
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

      <section className="section">
        <h2>Блок для копирования менеджеру</h2>
        <p>Единственная часть, которую РОП отправляет менеджеру. Без обвинений и без аналитики.</p>
        <div className="task">
          <div className="copybox">{copyText}</div>
          <button className="btn" onClick={props.onCopy}>
            Скопировать задачу менеджеру
          </button>
        </div>
      </section>

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

      <section className="section">
        <h2>Полный markdown-отчёт</h2>
        <p>Большой аудитный текст. Открывается только по запросу.</p>
        <button className="btn secondary" onClick={props.onToggleMarkdown} disabled={!meta?.report_id}>
          {props.showMarkdown ? 'Скрыть полный отчёт' : 'Показать полный отчёт'}
        </button>
        {props.showMarkdown && props.markdown && <div className="markdown">{props.markdown}</div>}
      </section>
    </>
  )
}
