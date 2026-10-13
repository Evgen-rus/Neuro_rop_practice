import { useEffect, useMemo, useRef, useState } from 'react'
import { formatMoscowDateTime } from './dateTime'
import {
  searchCandidates,
  type AuthUser,
  type Candidate,
  type JobState,
} from './api'
import {
  buildLeadControlSearchRequest,
  leadCategoryLabel,
  leadClosureLabel,
  leadTechnicalSummary,
  MAX_LEAD_CONTROL_SELECTION,
  toggleLeadSelection,
  type LeadControlGroup,
} from './leadControlView'
import './LeadControl.css'

type Props = {
  user: AuthUser
  managers: Array<[string, string]>
  job: JobState | null
  historyRevision?: number
  onOpenReport: (reportId: number, managerName?: string) => Promise<void>
  onOpenHistory?: (candidateId: string) => Promise<void> | void
  onStartAnalysis: (ids: string[]) => Promise<void>
}

const ANALYSIS_STATUS: Record<string, { label: string; tone: string; kind: string }> = {
  not_analyzed: { label: 'Нет анализа', tone: 'neutral', kind: 'not_analyzed' },
  running: { label: 'Выполняется', tone: 'running', kind: 'running' },
  ready: { label: 'Отчёт готов', tone: 'ready', kind: 'ready' },
  error: { label: 'Ошибка анализа', tone: 'error', kind: 'error' },
}

const PROGRESS_STAGE: Record<string, string> = {
  queued: 'в очереди',
  crm_context: 'сбор истории CRM',
  audio_lookup: 'поиск звонков',
  audio_download: 'загрузка звонков',
  transcription: 'транскрибация',
  llm_analysis: 'анализ',
  validation: 'проверка отчёта',
  report: 'сохранение отчёта',
}

const STAGE_REVIEW_VERDICT: Record<string, string> = {
  confirmed_correct: 'Этап подтверждён фактами',
  disputed: 'Этап расходится с фактами',
  insufficient_evidence: 'Недостаточно данных по этапу',
}

function dateLabel(value: string | null | undefined): string {
  if (!value) return 'не указана'
  return formatMoscowDateTime(value, { day: '2-digit', month: 'short', year: 'numeric' }) || value
}

function analysisState(candidate: Candidate, job: JobState | null) {
  const progress = job?.entity_progress?.[`lead:${candidate.entity_id}`]
  const result = job?.results?.find((item) => item.entity_type === 'lead' && item.entity_id === candidate.entity_id)

  if (progress && ['queued', 'running'].includes(progress.status)) {
    const stage = PROGRESS_STAGE[progress.stage]
    return { label: stage ? `Выполняется · ${stage}` : 'Выполняется', tone: 'running', kind: 'running' }
  }
  if (progress && ['error', 'failed'].includes(progress.status)) {
    return { label: 'Ошибка анализа', tone: 'error', kind: 'error' }
  }
  if (result) {
    return result.has_analysis
      ? { label: 'Отчёт готов', tone: 'ready', kind: 'ready' }
      : { label: 'Анализ завершён без отчёта', tone: 'neutral', kind: 'neutral' }
  }

  const status = candidate.analysis_status || (candidate.analyzed || candidate.lead_analysis_available ? 'ready' : 'not_analyzed')
  return ANALYSIS_STATUS[status] || ANALYSIS_STATUS.not_analyzed
}

export function LeadControl({ user, managers, job, historyRevision = 0, onOpenReport, onOpenHistory, onStartAnalysis }: Props) {
  const [group, setGroup] = useState<LeadControlGroup>('active')
  const [managerId, setManagerId] = useState('')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshError, setRefreshError] = useState('')
  const [actionError, setActionError] = useState('')
  const [cacheReloadKey, setCacheReloadKey] = useState(0)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [starting, setStarting] = useState(false)
  const [refreshingCrm, setRefreshingCrm] = useState(false)
  const [openingReportId, setOpeningReportId] = useState<number | null>(null)
  const [openingHistoryId, setOpeningHistoryId] = useState<string | null>(null)
  const [loadedScope, setLoadedScope] = useState('')
  const [crmUpdatedAt, setCrmUpdatedAt] = useState<string | null>(null)
  const [crmSnapshotAvailable, setCrmSnapshotAvailable] = useState(false)
  const [managerNames, setManagerNames] = useState<Record<string, string>>({})

  const managerOptions = useMemo(() => {
    const names = new Map<string, string>()
    const fallbackNames = new Map(managers)
    for (const [id, name] of Object.entries(managerNames)) {
      const cachedName = name.trim()
      const fallbackName = fallbackNames.get(id)
      if (cachedName) names.set(id, cachedName)
      else if (fallbackName) names.set(id, fallbackName)
    }
    for (const candidate of candidates) {
      const id = candidate.manager_id
      if (!id || names.has(id)) continue
      const candidateName = candidate.manager_name?.trim()
      const fallbackName = fallbackNames.get(id)
      if (candidateName) names.set(id, candidateName)
      else if (fallbackName) names.set(id, fallbackName)
    }
    return [...names.entries()].sort((left, right) => left[1].localeCompare(right[1], 'ru'))
  }, [candidates, managerNames, managers])
  const terminalJobId = job && ['done', 'error'].includes(job.status) ? job.job_id : ''
  const scopeKey = `${group}:${managerId}`
  const scopeKeyRef = useRef(scopeKey)
  scopeKeyRef.current = scopeKey
  const rowsVisible = loadedScope === scopeKey
  const refreshBlocked = loading || refreshingCrm || (Boolean(loadedScope) && !rowsVisible)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    void searchCandidates(buildLeadControlSearchRequest(group, managerId, false))
      .then((response) => {
        if (!cancelled) {
          const nextCandidates = response.candidates || []
          setCandidates(nextCandidates)
          setManagerNames(response.manager_names || {})
          setLoadedScope(`${group}:${managerId}`)
          setCrmUpdatedAt(response.crm_updated_at || null)
          setCrmSnapshotAvailable(response.crm_snapshot_available === true)
          const visibleIds = new Set(nextCandidates.map((candidate) => candidate.entity_id))
          setSelectedIds((current) => current.filter((id) => visibleIds.has(id)))
        }
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить лиды')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [group, managerId, cacheReloadKey, historyRevision])

  useEffect(() => {
    if (terminalJobId) setCacheReloadKey((value) => value + 1)
  }, [terminalJobId])

  useEffect(() => {
    setSelectedIds([])
    setActionError('')
    setRefreshError('')
  }, [group, managerId])

  const jobRunning = Boolean(job && ['queued', 'running'].includes(job.status))
  const runDisabled = !selectedIds.length || starting || jobRunning || refreshingCrm || loading || !rowsVisible
  const visibleCandidates = rowsVisible ? candidates : []
  const candidateSummary = visibleCandidates.reduce((summary, candidate) => {
    const state = analysisState(candidate, job)
    if (state.kind === 'not_analyzed') summary.notAnalyzed += 1
    else if (state.kind === 'ready') summary.ready += 1
    else if (state.kind === 'error') summary.errors += 1
    return summary
  }, { notAnalyzed: 0, ready: 0, errors: 0 })
  const kpis = [
    ['◇', 'В списке', visibleCandidates.length, 'blue'],
    ['○', 'Без анализа', candidateSummary.notAnalyzed, 'blue'],
    ['✓', 'Отчёты готовы', candidateSummary.ready, 'green'],
    ['!', 'Ошибки анализа', candidateSummary.errors, 'red'],
  ] as const

  function toggle(id: string) {
    setSelectedIds((current) => toggleLeadSelection(current, id))
  }

  async function openReport(reportId: number) {
    setOpeningReportId(reportId)
    setActionError('')
    try {
      const candidate = candidates.find((item) => item.latest_report_id === reportId)
      const managerName = candidate?.manager_name?.trim()
        || (candidate?.manager_id ? managerNames[candidate.manager_id] || managers.find(([id]) => id === candidate.manager_id)?.[1] : '')
      await onOpenReport(reportId, managerName || undefined)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : 'Не удалось открыть отчёт')
    } finally {
      setOpeningReportId(null)
    }
  }

  async function openHistory(candidateId: string) {
    if (!onOpenHistory) return
    setOpeningHistoryId(candidateId)
    setActionError('')
    try {
      await onOpenHistory(candidateId)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : 'Не удалось открыть историю лида')
    } finally {
      setOpeningHistoryId(null)
    }
  }

  async function refreshBitrix() {
    if (refreshBlocked) return
    const requestedScope = scopeKey
    setRefreshingCrm(true)
    setRefreshError('')
    try {
      const response = await searchCandidates(buildLeadControlSearchRequest(group, managerId, true))
      if (scopeKeyRef.current !== requestedScope) return
      const nextCandidates = response.candidates || []
      setCandidates(nextCandidates)
      setManagerNames(response.manager_names || {})
      setLoadedScope(requestedScope)
      setCrmUpdatedAt(response.crm_updated_at || null)
      setCrmSnapshotAvailable(response.crm_snapshot_available === true)
      setError('')
      const visibleIds = new Set(nextCandidates.map((candidate) => candidate.entity_id))
      setSelectedIds((current) => current.filter((id) => visibleIds.has(id)))
    } catch (reason) {
      if (scopeKeyRef.current === requestedScope) {
        setRefreshError(reason instanceof Error ? reason.message : 'Не удалось обновить лиды из Bitrix')
      }
    } finally {
      setRefreshingCrm(false)
    }
  }

  async function startAnalysis() {
    if (runDisabled) return
    setStarting(true)
    setActionError('')
    try {
      await onStartAnalysis(selectedIds)
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : 'Не удалось запустить анализ')
    } finally {
      setStarting(false)
    }
  }

  if (user.role === 'manager') return null

  const crmUpdateLabel = crmUpdatedAt
    ? formatMoscowDateTime(crmUpdatedAt, { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) || crmUpdatedAt
    : null

  return <section className="lead-control" aria-labelledby="lead-control-title">
    <header className="lead-control-header">
      <div>
        <h1 id="lead-control-title">Контроль лидов</h1>
        <p>Лиды, созданные за последние 30 дней. Проверяйте обработку и обоснованность закрытия.</p>
      </div>
      <div className="lead-control-header-actions">
        <button type="button" className="dc-button" disabled={refreshBlocked} onClick={() => void refreshBitrix()}>{refreshingCrm ? 'Обновляем Bitrix…' : 'Обновить Bitrix'}</button>
        <p className="lead-control-source" aria-live="polite">{crmUpdateLabel ? `Bitrix обновлён ${crmUpdateLabel}` : crmSnapshotAvailable ? 'Сохранённые данные Bitrix' : 'Данные Bitrix ещё не загружены'}</p>
      </div>
    </header>

    {rowsVisible && crmSnapshotAvailable ? <section className="lead-control-kpis" aria-label="Показатели текущего списка лидов">
      <div className="lead-control-kpi-heading"><h2>Готовность анализа</h2><span>с учётом вкладки и фильтра</span></div>
      <div className="dc-kpis tasks">
        {kpis.map(([icon, label, value, tone]) => <article className={tone} key={label}>
          <span aria-hidden="true">{icon}</span><div><small>{label}</small><strong>{value}</strong></div>
        </article>)}
      </div>
    </section> : null}

    <div className="lead-control-toolbar">
      <div className="lead-control-tabs" role="tablist" aria-label="Группа лидов">
        <button type="button" role="tab" aria-selected={group === 'active'} className={group === 'active' ? 'active' : ''} onClick={() => setGroup('active')}>В работе</button>
        <button type="button" role="tab" aria-selected={group === 'closed'} className={group === 'closed' ? 'active' : ''} onClick={() => setGroup('closed')}>Закрытые</button>
      </div>
      <label className="lead-control-manager">
        <select value={managerId} onChange={(event) => setManagerId(event.target.value)} aria-label="Фильтр по менеджеру">
          <option value="">Все менеджеры</option>
          {managerOptions.map(([id, name]) => <option value={id} key={id}>{name}</option>)}
        </select>
      </label>
    </div>

    {jobRunning ? <p className="lead-control-run-status" role="status">Обработка лидов идёт. Статус каждого лида обновляется отдельно.</p> : null}
    {error ? <div className="lead-control-error" role="alert"><span>{error}</span><button type="button" className="dc-button" onClick={() => setCacheReloadKey((value) => value + 1)}>Повторить загрузку</button></div> : null}
    {refreshError ? <p className="lead-control-action-error" role="alert">{refreshError}</p> : null}
    {actionError ? <p className="lead-control-action-error" role="alert">{actionError}</p> : null}

    <div className="lead-control-analysis-actions">
      <span role="status" aria-live="polite">Выбрано {selectedIds.length}/{MAX_LEAD_CONTROL_SELECTION}</span>
      <button type="button" className="dc-button primary" disabled={runDisabled} onClick={() => void startAnalysis()}>
        {starting ? 'Запускаем…' : selectedIds.length ? `Запустить анализ (${selectedIds.length})` : 'Запустить анализ'}
      </button>
    </div>

    <div className="dc-table-wrap lead-control-table-wrap" aria-busy={loading || refreshingCrm}>
      <div className="dc-table-scroll lead-control-table-scroll" role="table" aria-label="Лиды текущей выборки">
        <div className="dc-lead-columns" role="row">
          <span role="columnheader" aria-label="Выбор лида" />
          <span role="columnheader">Лид и ответственный</span>
          <span role="columnheader">Что требует внимания</span>
          <span role="columnheader">Что делать</span>
        </div>
        {loading && !rowsVisible ? <div className="lead-control-empty-row" role="row"><p className="dc-empty lead-control-empty" role="cell" aria-colspan={4}>Загружаем сохранённый список…</p></div> : null}
        {rowsVisible && candidates.length === 0 ? <div className="lead-control-empty-row" role="row"><p className="dc-empty lead-control-empty" role="cell" aria-colspan={4}>{crmSnapshotAvailable ? group === 'active' ? 'Лидов в работе за последние 30 дней не найдено.' : 'Закрытых лидов за последние 30 дней не найдено.' : 'Обновите Bitrix, чтобы загрузить лиды.'}</p></div> : null}
        {rowsVisible ? candidates.map((candidate) => {
        const reportId = candidate.latest_report_id
        const closure = leadClosureLabel(group, candidate.lead_closure_verdict)
        const state = analysisState(candidate, job)
        const currentTask = candidate.crm_task_checked === true ? candidate.crm_task : null
        const qualification = candidate.lead_qualification
        const managerName = candidate.manager_name?.trim()
          || (candidate.manager_id ? managerNames[candidate.manager_id] : '')
          || managerOptions.find(([id]) => id === candidate.manager_id)?.[1]
          || (candidate.manager_id ? 'Имя не найдено' : 'Не назначен')
        const selected = selectedIds.includes(candidate.entity_id)

        return <article className={`dc-lead-row ${selected ? 'selected' : ''}`} role="row" key={candidate.entity_id}>
          <div className="lead-control-select-cell" role="cell"><label className="lead-control-select" aria-label={`Выбрать лид ${candidate.title || candidate.entity_id} для анализа`}>
            <input
              type="checkbox"
              checked={selected}
              disabled={starting || jobRunning || refreshingCrm || (!selected && selectedIds.length >= MAX_LEAD_CONTROL_SELECTION)}
              onChange={() => toggle(candidate.entity_id)}
              aria-label={`Выбрать лид ${candidate.title || candidate.entity_id} для анализа`}
            />
          </label></div>
          <div className="lead-control-main" role="cell">
            <span className="lead-control-cell-label">Лид и ответственный</span>
            <div className="lead-control-title-line">
              <h2>{candidate.title || `Лид #${candidate.entity_id}`}</h2>
            </div>
            <div className="lead-control-id">
              <span>Лид #{candidate.entity_id}</span>
              <time dateTime={candidate.date_create || undefined}>Создан {dateLabel(candidate.date_create)}</time>
            </div>
            <div className="lead-control-owner-meta">
              <span><span className="lead-control-meta-label">Ответственный:</span> {managerName}</span>
              <span><span className="lead-control-meta-label">Этап:</span> <span className="lead-control-stage">{candidate.status || 'Этап не указан'}</span></span>
            </div>
            <div className="lead-control-main-actions">
              <div className="lead-control-main-buttons">
                {!(reportId && state.kind === 'ready') ? <span className={`lead-control-status ${state.tone}`}>{state.label}</span> : null}
                {reportId && state.kind === 'ready' ? <button type="button" className="dc-button primary" disabled={openingReportId === reportId} onClick={() => void openReport(reportId)}>{openingReportId === reportId ? 'Открываем…' : 'Открыть отчёт'}</button> : null}
                {onOpenHistory ? <button type="button" className="dc-button" disabled={openingHistoryId === candidate.entity_id} onClick={() => void openHistory(candidate.entity_id)}>{openingHistoryId === candidate.entity_id ? 'Открываем…' : 'История'}</button> : <span className="lead-control-muted">История недоступна</span>}
              </div>
              {candidate.analyzed_at ? <time className="lead-control-analyzed-at" dateTime={candidate.analyzed_at}>Анализ: {dateLabel(candidate.analyzed_at)}</time> : null}
            </div>
          </div>
          <div className="lead-control-assessment" role="cell">
            <span className="lead-control-cell-label">Что требует внимания</span>
            {state.kind === 'not_analyzed' ? <p className="lead-control-assessment-empty not-analyzed"><strong>Анализ не проведён.</strong> Отметьте лид и нажмите «Запустить анализ».</p> : !reportId && state.kind !== 'ready' ? <p className="lead-control-assessment-empty">{state.label}. Оценка обработки пока недоступна.</p> : <>
              <div className="lead-control-assessment-item">
                <span className="lead-control-assessment-label">Проблема</span>
                {candidate.lead_issue?.trim() ? <details className="lead-control-text-disclosure">
                  <summary><span className="lead-control-text">{candidate.lead_issue.trim()}</span><span className="lead-control-text-toggle"><span>Развернуть</span><span>Свернуть</span></span></summary>
                </details> : <p>В отчёте не указана</p>}
              </div>
              <details className="lead-control-analysis-details">
                <summary>Подробности анализа</summary>
                <div className="lead-control-analysis-details-list">
                  <div className="lead-control-assessment-item">
                    <span className="lead-control-assessment-label">Категория</span>
                    <span>{leadCategoryLabel(candidate.lead_category ?? qualification?.category)}</span>
                  </div>
                  <div className="lead-control-assessment-item">
                    <span className="lead-control-assessment-label">Проверка этапа</span>
                    <span>{STAGE_REVIEW_VERDICT[qualification?.stage_verdict || ''] || (qualification?.regulation_version ? 'Нет данных по этапу' : qualification ? 'Не сохранена в старом отчёте' : 'Нет данных по этапу')}</span>
                  </div>
                  <div className="lead-control-assessment-item">
                    <span className="lead-control-assessment-label">Техническое задание</span>
                    <span>{leadTechnicalSummary(qualification)}</span>
                  </div>
                  {qualification?.regulation_version ? <span className="lead-control-assessment-version">AutoBANT {qualification.regulation_version}</span> : null}
                  {qualification?.stage_reason?.trim() ? <div className="lead-control-stage-reason"><strong>Пояснение по этапу</strong><p>{qualification.stage_reason}</p></div> : null}
                  {reportId && state.kind === 'ready' && closure && candidate.lead_closure_reason?.trim() ? <div className="lead-control-stage-reason"><strong>Пояснение закрытия</strong><p>{candidate.lead_closure_reason}</p></div> : null}
                </div>
              </details>
            </>}
            {reportId && state.kind === 'ready' && closure ? <div className="lead-control-assessment-closure">
              <span className="lead-control-assessment-label">Оценка закрытия</span>
              <strong>{closure}</strong>
            </div> : null}
          </div>
          <div className="lead-control-next-action" role="cell">
            <span className="lead-control-cell-label">Что делать</span>
            <div className={`lead-control-next-action-recommendation ${state.kind !== 'not_analyzed' && candidate.lead_next_action?.trim() ? 'has-recommendation' : ''}`}>
              <span>Рекомендация</span>
              {state.kind !== 'not_analyzed' && candidate.lead_next_action?.trim() ? <details className="lead-control-text-disclosure">
                <summary><span className="lead-control-text">{candidate.lead_next_action}</span><span className="lead-control-text-toggle"><span>Развернуть</span><span>Свернуть</span></span></summary>
              </details> : <span className="lead-control-muted lead-control-no-recommendation">{state.kind === 'not_analyzed' ? 'Появится после анализа' : state.kind === 'ready' ? 'В отчёте нет рекомендации' : 'Рекомендация пока недоступна'}</span>}
            </div>
            <div className="lead-control-bitrix-task">
              <strong>Задача Bitrix</strong>
              {currentTask ? <div>
                <details className="lead-control-expandable"><summary>{currentTask.subject}</summary></details>
                {currentTask.deadline ? <time dateTime={currentTask.deadline}>Срок: {dateLabel(currentTask.deadline)}</time> : <span>Срок не указан</span>}
              </div> : <span>{candidate.crm_task_checked ? 'Активная задача не найдена' : 'Наличие задачи ещё не проверено'}</span>}
            </div>
          </div>
        </article>
        }) : null}
      </div>
    </div>

  </section>
}
