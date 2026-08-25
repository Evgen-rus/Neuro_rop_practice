import { useCallback, useEffect, useState } from 'react'
import {
  createPromptLabSnapshot,
  deletePromptLabVersion,
  fetchPromptLabBootstrap,
  fetchPromptLabExport,
  fetchPromptLabJob,
  fetchPromptLabRun,
  fetchPromptLabRuns,
  patchPromptLabVersion,
  savePromptLabReview,
  savePromptLabVersion,
  startPromptLabRun,
  type ManagerAssistantMode,
  type ManagerCompanionRecord,
  type ManagerFollowupsRecord,
  type ManagerFullScriptContent,
  type ManagerQuickHelpContent,
  type ManagerQuickHelpStrategy,
  type PromptLabBootstrap,
  type PromptLabBranch,
  type PromptLabJob,
  type PromptLabModuleKey,
  type PromptLabRun,
  type PromptLabVersion,
} from './api'
import { formatMoscowDateTime } from './dateTime'
import { CompanionResultView, FollowupsResultView, FullScriptResultView, QuickHelpResultView } from './managerResults'

type Layout = 'current' | 'experiment' | 'both'
type PendingNav = { kind: 'module' | 'leave'; next?: PromptLabModuleKey } | null

const MODULES: Array<{ key: PromptLabModuleKey; label: string }> = [
  { key: 'quick_help.push', label: 'Дожим' },
  { key: 'quick_help.reanimator', label: 'Реаниматор' },
  { key: 'full_script.message', label: 'Message' },
  { key: 'full_script.call', label: 'Call' },
  { key: 'full_script.email', label: 'Email' },
  { key: 'followups', label: 'Followups' },
  { key: 'companion', label: 'Companion' },
]

function moscowStamp(value?: string | null) {
  if (!value) return '—'
  return formatMoscowDateTime(value, { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) || value
}

export function PromptLabWorkspace({
  dealId,
  productionMode,
  question,
  onQuestion,
  onCopy,
  leaveRequest,
  onLeaveAttempt,
  onConfirmLeave,
}: {
  dealId: string
  productionMode: ManagerAssistantMode
  question: string
  onQuestion: (value: string) => void
  onCopy: (text: string, label: string) => Promise<void>
  leaveRequest?: number
  onLeaveAttempt?: (blocked: boolean) => void
  onConfirmLeave?: () => void
}) {
  const [moduleKey, setModuleKey] = useState<PromptLabModuleKey>(productionMode === 'push' ? 'quick_help.push' : 'quick_help.reanimator')
  const [bootstrap, setBootstrap] = useState<PromptLabBootstrap | null>(null)
  const [layout, setLayout] = useState<Layout>('both')
  const [currentPrompt, setCurrentPrompt] = useState('')
  const [experimentPrompt, setExperimentPrompt] = useState('')
  const [savedExperiment, setSavedExperiment] = useState('')
  const [currentModel, setCurrentModel] = useState('')
  const [experimentModel, setExperimentModel] = useState('')
  const [currentReasoning, setCurrentReasoning] = useState('')
  const [experimentReasoning, setExperimentReasoning] = useState('')
  const [currentRun, setCurrentRun] = useState<PromptLabRun | null>(null)
  const [experimentRun, setExperimentRun] = useState<PromptLabRun | null>(null)
  const [currentJob, setCurrentJob] = useState<PromptLabJob | null>(null)
  const [experimentJob, setExperimentJob] = useState<PromptLabJob | null>(null)
  const [strategy, setStrategy] = useState<ManagerQuickHelpStrategy>('primary')
  const [advanced, setAdvanced] = useState<'current' | 'experiment' | null>(null)
  const [error, setError] = useState('')
  const [versions, setVersions] = useState<PromptLabVersion[]>([])
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null)
  const [note, setNote] = useState('')
  const [pending, setPending] = useState<PendingNav>(null)
  const [existingJob, setExistingJob] = useState<PromptLabJob | null>(null)
  const [history, setHistory] = useState<PromptLabRun[]>([])
  const [managerNote, setManagerNote] = useState('')
  const [previousMessage, setPreviousMessage] = useState('')
  const [qhUpstream, setQhUpstream] = useState<{ current: number | null; experiment: number | null }>({ current: null, experiment: null })
  const unsaved = experimentPrompt !== savedExperiment

  useEffect(() => { onLeaveAttempt?.(unsaved) }, [onLeaveAttempt, unsaved])

  useEffect(() => {
    if (!leaveRequest) return
    if (unsaved) setPending({ kind: 'leave' })
    else onConfirmLeave?.()
  }, [leaveRequest, onConfirmLeave, unsaved])

  const loadBootstrap = useCallback(async (nextModule: PromptLabModuleKey) => {
    const payload = await fetchPromptLabBootstrap(dealId, nextModule)
    setBootstrap(payload)
    setCurrentPrompt(payload.production_current.prompt_template)
    setExperimentPrompt(payload.production_current.prompt_template)
    setSavedExperiment(payload.production_current.prompt_template)
    setCurrentModel(payload.runtime.model)
    setExperimentModel(payload.runtime.model)
    setCurrentReasoning(payload.runtime.reasoning)
    setExperimentReasoning(payload.runtime.reasoning)
    setVersions(payload.versions)
    setCurrentRun(null)
    setExperimentRun(null)
    if (payload.production_current.exists && payload.production_current.entry && nextModule.startsWith('quick_help.')) {
      setCurrentRun({
        id: 0,
        deal_id: dealId,
        module_key: nextModule,
        branch: 'current',
        snapshot_id: payload.snapshot.id || 0,
        prompt_hash: payload.production_current.prompt_hash,
        model: payload.production_current.model,
        reasoning: payload.production_current.reasoning,
        question: '',
        status: 'success',
        result: (payload.production_current.entry as { content: ManagerQuickHelpContent }).content as unknown as Record<string, unknown>,
        created_at: (payload.production_current.entry as { created_at?: string }).created_at || '',
      })
    }
    const runs = await fetchPromptLabRuns({ deal_id: dealId, module_key: nextModule })
    setHistory(runs.runs)
    if (nextModule.startsWith('full_script.') || nextModule.startsWith('quick_help.')) {
      const [pushRuns, reanimatorRuns] = await Promise.all([
        fetchPromptLabRuns({ deal_id: dealId, module_key: 'quick_help.push' }),
        fetchPromptLabRuns({ deal_id: dealId, module_key: 'quick_help.reanimator' }),
      ])
      const successful = [...pushRuns.runs, ...reanimatorRuns.runs].filter((item) => item.status === 'success' && item.id > 0)
      setQhUpstream({
        current: successful.find((item) => item.branch === 'current')?.id || null,
        experiment: successful.find((item) => item.branch === 'experiment')?.id || null,
      })
    }
  }, [dealId])

  useEffect(() => {
    void loadBootstrap(moduleKey).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [dealId, loadBootstrap, moduleKey])

  useEffect(() => {
    const jobs: Array<['current' | 'experiment', PromptLabJob | null]> = [['current', currentJob], ['experiment', experimentJob]]
    const timers: number[] = []
    for (const [branch, job] of jobs) {
      if (!job || !['queued', 'running'].includes(job.status)) continue
      const timer = window.setTimeout(async () => {
        const next = await fetchPromptLabJob(job.job_id)
        if (branch === 'current') setCurrentJob(next)
        else setExperimentJob(next)
        if (next.run) {
          if (branch === 'current') setCurrentRun(next.run)
          else setExperimentRun(next.run)
          if (moduleKey.startsWith('quick_help.') && next.run.id > 0) {
            setQhUpstream((value) => ({ ...value, [branch]: next.run?.id || null }))
          }
        } else if (next.run_id) {
          const run = await fetchPromptLabRun(next.run_id)
          if (branch === 'current') setCurrentRun(run)
          else setExperimentRun(run)
          if (moduleKey.startsWith('quick_help.') && run.id > 0) {
            setQhUpstream((value) => ({ ...value, [branch]: run.id }))
          }
        }
        if (next.status === 'done' || next.status === 'error' || next.status === 'exists') {
          const runs = await fetchPromptLabRuns({ deal_id: dealId, module_key: moduleKey })
          setHistory(runs.runs)
        }
      }, 800)
      timers.push(timer)
    }
    return () => { timers.forEach((timer) => window.clearTimeout(timer)) }
  }, [currentJob, dealId, experimentJob, moduleKey])

  const models = bootstrap?.models || []
  const snapshotLabel = moscowStamp(bootstrap?.snapshot.created_at)
  const family = moduleKey.startsWith('quick_help.') ? 'quick_help' : moduleKey.startsWith('full_script.') ? 'full_script' : moduleKey

  function reasoningFor(modelId: string) {
    return models.find((item) => item.id === modelId)?.reasoning || ['low']
  }

  function changeModule(next: PromptLabModuleKey) {
    if (unsaved) {
      setPending({ kind: 'module', next })
      return
    }
    setModuleKey(next)
  }

  async function refreshSnapshot() {
    await createPromptLabSnapshot(dealId)
    await loadBootstrap(moduleKey)
  }

  async function generate(branch: PromptLabBranch, force = false) {
    if (!bootstrap?.gate.ok && family !== 'companion') {
      setError(bootstrap?.gate.reason || 'Prompt Lab заблокирован')
      return
    }
    setError('')
    const isCurrent = branch === 'current'
    const body = {
      deal_id: dealId,
      module_key: moduleKey,
      branch,
      snapshot_id: bootstrap?.snapshot.id,
      prompt_template: isCurrent ? currentPrompt : experimentPrompt,
      prompt_version_id: !isCurrent ? selectedVersionId : null,
      model: isCurrent ? currentModel : experimentModel,
      reasoning: isCurrent ? currentReasoning : experimentReasoning,
      question,
      selected_strategy: family === 'full_script' ? strategy : null,
      upstream_run_id: family === 'full_script' ? (isCurrent ? qhUpstream.current : qhUpstream.experiment) : null,
      manager_note: family === 'companion' ? managerNote : '',
      previous_message: family === 'companion' ? previousMessage : '',
      reuse_existing: force ? false : null,
    }
    if (family === 'full_script' && (!body.upstream_run_id || body.upstream_run_id <= 0)) {
      setError('Сначала сгенерируйте Quick Help этой ветки')
      return
    }
    const job = await startPromptLabRun(body)
    if (job.status === 'exists') {
      setExistingJob(job)
      return
    }
    if (isCurrent) setCurrentJob(job)
    else setExperimentJob(job)
    if (job.run) {
      if (isCurrent) setCurrentRun(job.run)
      else setExperimentRun(job.run)
      if (family === 'quick_help' && job.run.id > 0) {
        setQhUpstream((value) => ({ ...value, [branch]: job.run?.id || null }))
      }
    }
  }

  async function generateBoth() {
    await Promise.all([generate('current', true), generate('experiment', true)])
  }

  async function saveVersion() {
    const version = await savePromptLabVersion({
      prompt_key: moduleKey,
      prompt_text: experimentPrompt,
      based_on_id: selectedVersionId,
      note: note || null,
    })
    setSelectedVersionId(version.id)
    setSavedExperiment(experimentPrompt)
    setVersions((items) => [version, ...items.filter((item) => item.id !== version.id)])
  }

  function downloadMarkdown(name: string, text: string) {
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  async function exportVersions(mode: 'current' | 'candidates_module' | 'candidates_all') {
    const payload = await fetchPromptLabExport(mode, moduleKey)
    const lines = ['# Prompt Lab candidates', '']
    for (const item of payload.items) {
      lines.push(`## ${String(item.prompt_key)} v${String(item.version)}`)
      lines.push(`- created_at: ${String(item.created_at)}`)
      lines.push(`- candidate: ${item.candidate ? 'yes' : 'no'}`)
      lines.push(`- verified: ${item.verified ? 'yes' : 'no'}`)
      if (item.note) lines.push(`- note: ${String(item.note)}`)
      lines.push(`- base_production_hash: ${String(item.base_production_hash || '')}`)
      lines.push(`- schema: ${String(item.schema_version || '')}`)
      lines.push(`- models: ${Array.isArray(item.models_tested) ? item.models_tested.join(', ') : ''}`)
      const stats = item.review_stats && typeof item.review_stats === 'object' ? item.review_stats as Record<string, number> : {}
      lines.push(`- EXPERIMENT лучше: ${stats.experiment_better || 0} / ${stats.total || 0}`)
      lines.push('')
      lines.push(String(item.prompt_text || ''))
      lines.push('')
    }
    downloadMarkdown(`prompt-lab-${mode}.md`, lines.join('\n'))
  }

  const currentBusy = Boolean(currentJob && ['queued', 'running'].includes(currentJob.status))
  const experimentBusy = Boolean(experimentJob && ['queued', 'running'].includes(experimentJob.status))

  return <div className="dc-prompt-lab">
    {!bootstrap?.gate.ok && family !== 'companion' ? <p className="dc-manager-error" role="alert">{bootstrap?.gate.reason || 'Prompt Lab заблокирован'}</p> : null}
    {bootstrap?.production_current.stale ? <p className="dc-prompt-lab-stale">Создан на предыдущем контексте</p> : null}
    {error ? <p className="dc-manager-error">{error}</p> : null}
    <div className="dc-prompt-lab-toolbar">
      <label>Модуль
        <select value={moduleKey} onChange={(event) => changeModule(event.target.value as PromptLabModuleKey)}>
          {MODULES.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
        </select>
      </label>
      <div className="dc-manager-mode-switch" role="tablist" aria-label="Отображение">
        <button type="button" className={layout === 'current' ? 'active' : ''} onClick={() => setLayout('current')}>Current</button>
        <button type="button" className={layout === 'experiment' ? 'active' : ''} onClick={() => setLayout('experiment')}>Experiment</button>
        <button type="button" className={layout === 'both' ? 'active' : ''} onClick={() => setLayout('both')}>Рядом</button>
      </div>
      <span>Контекст: {snapshotLabel} МСК</span>
      <label className="dc-prompt-lab-question">Вопрос менеджера
        <input value={question} maxLength={4000} onChange={(event) => onQuestion(event.target.value)} placeholder="Один вопрос для CURRENT и EXPERIMENT" />
      </label>
      <button type="button" className="dc-button" onClick={() => void refreshSnapshot()}>Обновить</button>
      <button type="button" className="dc-button primary" disabled={currentBusy || experimentBusy} onClick={() => void generateBoth()}>Сгенерировать оба</button>
    </div>
    <div className={`dc-prompt-lab-grid ${layout}`}>
      {layout !== 'experiment' ? <BranchPanel
        title="CURRENT"
        readOnlyPrompt
        prompt={currentPrompt}
        onPrompt={setCurrentPrompt}
        model={currentModel}
        reasoning={currentReasoning}
        models={models}
        reasoningOptions={reasoningFor(currentModel)}
        onModel={setCurrentModel}
        onReasoning={setCurrentReasoning}
        busy={currentBusy}
        job={currentJob}
        run={currentRun}
        moduleKey={moduleKey}
        strategy={strategy}
        onStrategy={setStrategy}
        onGenerate={() => void generate('current')}
        onCopy={onCopy}
        advanced={advanced === 'current'}
        onToggleAdvanced={() => setAdvanced((value) => value === 'current' ? null : 'current')}
      /> : null}
      {layout !== 'current' ? <BranchPanel
        title="EXPERIMENT"
        prompt={experimentPrompt}
        onPrompt={setExperimentPrompt}
        model={experimentModel}
        reasoning={experimentReasoning}
        models={models}
        reasoningOptions={reasoningFor(experimentModel)}
        onModel={setExperimentModel}
        onReasoning={setExperimentReasoning}
        busy={experimentBusy}
        job={experimentJob}
        run={experimentRun}
        moduleKey={moduleKey}
        strategy={strategy}
        onStrategy={setStrategy}
        onGenerate={() => void generate('experiment')}
        onCopy={onCopy}
        advanced={advanced === 'experiment'}
        onToggleAdvanced={() => setAdvanced((value) => value === 'experiment' ? null : 'experiment')}
        extraActions={<>
          <button type="button" className="dc-link-button" onClick={() => { setExperimentPrompt(currentPrompt); setExperimentModel(currentModel); setExperimentReasoning(currentReasoning) }}>Копировать CURRENT → EXPERIMENT</button>
          <button type="button" className="dc-link-button" onClick={() => { setExperimentPrompt(bootstrap?.production_current.prompt_template || currentPrompt); setSavedExperiment(bootstrap?.production_current.prompt_template || currentPrompt) }}>Сбросить к CURRENT</button>
        </>}
      /> : null}
    </div>
    {family === 'companion' ? <div className="dc-prompt-lab-companion-inputs">
      <label>PREVIOUS_MESSAGE<textarea value={previousMessage} onChange={(event) => setPreviousMessage(event.target.value)} /></label>
      <label>MANAGER_NOTE<textarea value={managerNote} onChange={(event) => setManagerNote(event.target.value)} /></label>
    </div> : null}
    <div className="dc-prompt-lab-versions">
      <strong>Версии prompt</strong>
      <select value={selectedVersionId || ''} onChange={(event) => {
        const id = Number(event.target.value) || null
        setSelectedVersionId(id)
        const version = versions.find((item) => item.id === id)
        if (version) {
          if (unsaved) { setPending({ kind: 'module', next: moduleKey }); return }
          setExperimentPrompt(version.prompt_text)
          setSavedExperiment(version.prompt_text)
        }
      }}>
        <option value="">production / несохранённый</option>
        {versions.map((item) => <option key={item.id} value={item.id}>v{item.version_number} · {moscowStamp(item.created_at)} МСК{item.candidate ? ' ★' : ''}{item.verified ? ' ✓' : ''}</option>)}
      </select>
      <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Заметка к версии" />
      <button type="button" className="dc-button" onClick={() => void saveVersion()}>Сохранить новую версию</button>
      {selectedVersionId ? <>
        <button type="button" onClick={() => void patchPromptLabVersion(selectedVersionId, { candidate: true }).then((item) => setVersions((rows) => rows.map((row) => row.id === item.id ? item : row)))}>★ Кандидат</button>
        <button type="button" onClick={() => void patchPromptLabVersion(selectedVersionId, { verified: true }).then((item) => setVersions((rows) => rows.map((row) => row.id === item.id ? item : row)))}>✓ Проверен</button>
        <button type="button" onClick={() => void deletePromptLabVersion(selectedVersionId).then(() => setVersions((rows) => rows.filter((row) => row.id !== selectedVersionId)))}>Архивировать</button>
      </> : null}
      <button type="button" onClick={() => void exportVersions('current')}>Экспорт текущей</button>
      <button type="button" onClick={() => void exportVersions('candidates_module')}>Экспорт ★ модуля</button>
      <button type="button" onClick={() => void exportVersions('candidates_all')}>Экспорт всех ★</button>
    </div>
    {currentRun && currentRun.id > 0 && experimentRun && experimentRun.id > 0 ? <div className="dc-prompt-lab-review">
      <span>A/B</span>
      {([['current_better', 'CURRENT лучше'], ['same', 'Одинаково'], ['experiment_better', 'EXPERIMENT лучше'], ['both_bad', 'Оба плохие']] as const).map(([verdict, label]) => (
        <button key={verdict} type="button" onClick={() => void savePromptLabReview({ current_run_id: currentRun.id, experiment_run_id: experimentRun.id, prompt_version_id: selectedVersionId, verdict })}>{label}</button>
      ))}
    </div> : null}
    {history.length ? <details className="dc-prompt-lab-history"><summary>История runs</summary>
      <table><thead><tr><th>Время МСК</th><th>Ветка</th><th>Модель</th><th>Reasoning</th><th>Статус</th><th>ms</th><th>₽</th></tr></thead>
        <tbody>{history.map((item) => <tr key={item.id}><td>{moscowStamp(item.created_at)}</td><td>{item.branch}</td><td>{item.model}</td><td>{item.reasoning}</td><td>{item.status === 'success' ? '✓ Готово' : `✕ ${item.error || 'ошибка'}`}</td><td>{item.latency_seconds ? Math.round(item.latency_seconds * 1000) : '—'}</td><td>{String((item.cost as { estimated_cost_rub?: number } | null)?.estimated_cost_rub ?? '—')}</td></tr>)}</tbody>
      </table>
    </details> : null}
    {pending ? <div className="dc-prompt-lab-modal" role="dialog">
      <p>Есть несохранённые изменения.</p>
      <button type="button" className="dc-button primary" onClick={() => void saveVersion().then(() => {
        if (pending.kind === 'leave') onConfirmLeave?.()
        else if (pending.next) setModuleKey(pending.next)
        setPending(null)
      })}>Сохранить новую версию</button>
      <button type="button" className="dc-button" onClick={() => {
        setSavedExperiment(experimentPrompt)
        if (pending.kind === 'leave') onConfirmLeave?.()
        else if (pending.next) setModuleKey(pending.next)
        setPending(null)
      }}>Продолжить без сохранения</button>
      <button type="button" onClick={() => setPending(null)}>Отмена</button>
    </div> : null}
    {existingJob ? <div className="dc-prompt-lab-modal" role="dialog">
      <p>Такая конфигурация уже запускалась.</p>
      <button type="button" className="dc-button primary" onClick={() => {
        if (existingJob.run && existingJob.branch === 'current') setCurrentRun(existingJob.run)
        if (existingJob.run && existingJob.branch === 'experiment') setExperimentRun(existingJob.run)
        setExistingJob(null)
      }}>Использовать результат</button>
      <button type="button" className="dc-button" onClick={() => { const branch = existingJob.branch; setExistingJob(null); void generate(branch, true) }}>Запустить заново</button>
    </div> : null}
  </div>
}

function BranchPanel(props: {
  title: string
  readOnlyPrompt?: boolean
  prompt: string
  onPrompt: (value: string) => void
  model: string
  reasoning: string
  models: Array<{ id: string; label: string; reasoning: string[] }>
  reasoningOptions: string[]
  onModel: (value: string) => void
  onReasoning: (value: string) => void
  busy: boolean
  job: PromptLabJob | null
  run: PromptLabRun | null
  moduleKey: PromptLabModuleKey
  strategy: ManagerQuickHelpStrategy
  onStrategy: (value: ManagerQuickHelpStrategy) => void
  onGenerate: () => void
  onCopy: (text: string, label: string) => Promise<void>
  advanced: boolean
  onToggleAdvanced: () => void
  extraActions?: React.ReactNode
}) {
  const mode: ManagerAssistantMode = props.moduleKey === 'quick_help.push' ? 'push' : 'reanimator'
  return <section className="dc-prompt-lab-branch">
    <header>
      <strong>{props.title}</strong>
      <span>{props.job?.status === 'error' ? `✕ ${props.job.error || props.job.detail}` : props.busy ? props.job?.detail || 'Генерация…' : props.run?.status === 'success' ? '✓ Готово' : props.run?.status === 'error' ? `✕ ${props.run.error}` : 'Нет результата'}</span>
    </header>
    <label>Prompt
      <textarea readOnly={props.readOnlyPrompt} value={props.prompt} onChange={(event) => props.onPrompt(event.target.value)} />
    </label>
    <div className="dc-prompt-lab-config">
      <label>Model
        <select value={props.model} onChange={(event) => {
          props.onModel(event.target.value)
          const allowed = props.models.find((item) => item.id === event.target.value)?.reasoning || []
          if (!allowed.includes(props.reasoning)) props.onReasoning(allowed[0] || 'low')
        }}>
          {props.models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
      </label>
      <label>Reasoning
        <select value={props.reasoning} onChange={(event) => props.onReasoning(event.target.value)}>
          {props.reasoningOptions.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <button type="button" className="dc-button primary" disabled={props.busy} onClick={props.onGenerate}>Сгенерировать</button>
    </div>
    {props.extraActions}
    <LabResult run={props.run} moduleKey={props.moduleKey} mode={mode} strategy={props.strategy} onStrategy={props.onStrategy} onCopy={props.onCopy} />
    <button type="button" className="dc-link-button" onClick={props.onToggleAdvanced}>{props.advanced ? 'Скрыть Advanced' : 'Advanced'}</button>
    {props.advanced && props.run ? <pre className="dc-prompt-lab-advanced">{JSON.stringify({
      effective_prompt: props.run.effective_prompt,
      prompt_hash: props.run.prompt_hash,
      model: props.run.model,
      reasoning: props.run.reasoning,
      schema_version: props.run.schema_version,
      usage: props.run.usage,
      cost: props.run.cost,
      latency_seconds: props.run.latency_seconds,
      response_status: props.run.response_status,
      error: props.run.error,
    }, null, 2)}</pre> : null}
  </section>
}

function LabResult({
  run,
  moduleKey,
  mode,
  strategy,
  onStrategy,
  onCopy,
}: {
  run: PromptLabRun | null
  moduleKey: PromptLabModuleKey
  mode: ManagerAssistantMode
  strategy: ManagerQuickHelpStrategy
  onStrategy: (value: ManagerQuickHelpStrategy) => void
  onCopy: (text: string, label: string) => Promise<void>
}) {
  if (!run) return <p className="empty">Результат ещё не сформирован</p>
  if (run.status === 'error') return <p className="dc-manager-error">{run.error || 'Ошибка генерации'}</p>
  const result = run.result
  if (!result) return <p className="empty">Результат ещё не сформирован</p>
  if (moduleKey.startsWith('quick_help.')) {
    return <QuickHelpResultView
      entry={{ content: result as unknown as ManagerQuickHelpContent, created_at: run.created_at }}
      mode={mode}
      selectedStrategy={strategy}
      onSelectedStrategy={onStrategy}
      onCopy={onCopy}
    />
  }
  if (moduleKey === 'followups') {
    return <FollowupsResultView record={{ id: run.id, created_at: run.created_at, content: result as unknown as ManagerFollowupsRecord['content'] }} />
  }
  if (moduleKey === 'companion') {
    return <CompanionResultView companion={{ id: run.id, created_at: run.created_at, content: result as unknown as ManagerCompanionRecord['content'] } as ManagerCompanionRecord} onCopy={(text) => void onCopy(text, 'Сопроводительный текст')} />
  }
  return <FullScriptResultView script={result as unknown as ManagerFullScriptContent} onCopy={onCopy} />
}
