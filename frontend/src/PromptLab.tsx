import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createPromptLabSnapshot,
  deletePromptLabVersion,
  fetchPromptLabBootstrap,
  fetchPromptLabExport,
  fetchPromptLabJob,
  fetchPromptLabRawExchange,
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
  type PromptLabRawExchange,
  type PromptLabRun,
  type PromptLabVersion,
} from './api'
import { formatMoscowDateTime } from './dateTime'
import { CompanionResultView, FollowupsResultView, FullScriptResultView, QuickHelpResultView } from './managerResults'
import {
  PROMPT_LAB_LOADING_TEXT,
  jobMatchesModule,
  keepQuickHelpSource,
  labResultKind,
  materialSourceCaption,
  runMatchesModule,
  selectedClientMessage,
  strategyLabelFromResult,
  visibleLabRun,
} from './promptLabWorkspace'

type Layout = 'current' | 'experiment' | 'both'
type PendingNav =
  | { kind: 'module'; next: PromptLabModuleKey }
  | { kind: 'leave' }
  | { kind: 'version'; nextVersionId: number | null }

const MODULES: Array<{ key: PromptLabModuleKey; label: string }> = [
  { key: 'quick_help.push', label: 'Дожим' },
  { key: 'quick_help.reanimator', label: 'Реаниматор' },
  { key: 'full_script.message', label: 'Message' },
  { key: 'full_script.call', label: 'Call' },
  { key: 'full_script.email', label: 'Email' },
  { key: 'followups', label: 'Followups' },
  { key: 'companion', label: 'Companion' },
]

const SOURCE_STRATEGIES: ManagerQuickHelpStrategy[] = ['primary', 'alternative', 'pattern_break']
const QH_UPSTREAM_REQUIRED = 'Сначала получите Quick Help этой ветки на текущем контексте'

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
  const [rawExchange, setRawExchange] = useState<Record<PromptLabBranch, PromptLabRawExchange | null>>({ current: null, experiment: null })
  const [strategy, setStrategy] = useState<ManagerQuickHelpStrategy>('primary')
  const [advanced, setAdvanced] = useState<'current' | 'experiment' | null>(null)
  const [error, setError] = useState('')
  const [versions, setVersions] = useState<PromptLabVersion[]>([])
  const [selectedVersionId, setSelectedVersionId] = useState<number | null>(null)
  const [note, setNote] = useState('')
  const [pending, setPending] = useState<PendingNav | null>(null)
  const [existingJob, setExistingJob] = useState<PromptLabJob | null>(null)
  const [history, setHistory] = useState<PromptLabRun[]>([])
  const [managerNote, setManagerNote] = useState('')
  const [previousMessage, setPreviousMessage] = useState('')
  const [qhUpstream, setQhUpstream] = useState<{ current: number | null; experiment: number | null }>({ current: null, experiment: null })
  const [qhSource, setQhSource] = useState<{ current: PromptLabRun | null; experiment: PromptLabRun | null }>({ current: null, experiment: null })
  const [qhMode, setQhMode] = useState<ManagerAssistantMode>(productionMode === 'reanimator' ? 'reanimator' : 'push')
  const [workspaceLoading, setWorkspaceLoading] = useState(true)
  const unsaved = experimentPrompt !== savedExperiment
  const strategyRef = useRef(strategy)
  strategyRef.current = strategy
  const moduleKeyRef = useRef(moduleKey)
  moduleKeyRef.current = moduleKey
  const bootstrapSeqRef = useRef(0)

  useEffect(() => { onLeaveAttempt?.(unsaved) }, [onLeaveAttempt, unsaved])

  useEffect(() => {
    if (!leaveRequest) return
    if (unsaved) setPending({ kind: 'leave' })
    else onConfirmLeave?.()
  }, [leaveRequest, onConfirmLeave, unsaved])

  const loadBootstrap = useCallback(async (nextModule: PromptLabModuleKey, selectedStrategy?: ManagerQuickHelpStrategy, activeQhMode?: ManagerAssistantMode, requestId?: number) => {
    const seq = requestId ?? ++bootstrapSeqRef.current
    const mode = activeQhMode || qhMode
    const payload = await fetchPromptLabBootstrap(
      dealId,
      nextModule,
      nextModule.startsWith('full_script.') ? (selectedStrategy || 'primary') : null,
      nextModule.startsWith('full_script.') ? mode : null,
    )
    if (seq !== bootstrapSeqRef.current || moduleKeyRef.current !== nextModule || payload.module !== nextModule) return
    setBootstrap(payload)
    setCurrentPrompt(payload.production_current.prompt_template)
    setExperimentPrompt(payload.production_current.prompt_template)
    setSavedExperiment(payload.production_current.prompt_template)
    const useProduction = payload.production_current.exists || Boolean(payload.production_current.lab_run)
    const model = useProduction ? payload.production_current.model : payload.runtime.model
    const reasoning = useProduction ? payload.production_current.reasoning : payload.runtime.reasoning
    setCurrentModel(model)
    setExperimentModel(model)
    setCurrentReasoning(reasoning)
    setExperimentReasoning(reasoning)
    setVersions(payload.versions)
    setSelectedVersionId(null)
    const imported = visibleLabRun(payload.production_current.lab_run || null, nextModule)
    setCurrentRun(imported)
    setExperimentRun(null)
    const keepSource = nextModule.startsWith('full_script.')
    if (!keepSource) {
      setQhUpstream({ current: null, experiment: null })
    }
    const snapshotId = payload.snapshot.id || null
    const importedMatchesSnapshot = Boolean(imported && imported.id > 0 && (snapshotId == null || imported.snapshot_id === snapshotId))
    if (importedMatchesSnapshot && imported) {
      setQhUpstream((value) => ({
        current: nextModule.startsWith('quick_help.')
          ? imported.id
          : (value.current || imported.upstream_run_id || null),
        experiment: keepSource ? value.experiment : null,
      }))
    }
    setWorkspaceLoading(false)
    const runs = await fetchPromptLabRuns({ deal_id: dealId, module_key: nextModule })
    if (seq !== bootstrapSeqRef.current || moduleKeyRef.current !== nextModule) return
    setHistory(runs.runs)
    if (nextModule.startsWith('full_script.') || nextModule.startsWith('quick_help.')) {
      const qhKey = nextModule.startsWith('quick_help.') ? nextModule : (`quick_help.${mode}` as PromptLabModuleKey)
      const qhRuns = await fetchPromptLabRuns({
        deal_id: dealId,
        module_key: qhKey,
        ...(snapshotId ? { snapshot_id: snapshotId } : {}),
      })
      if (seq !== bootstrapSeqRef.current || moduleKeyRef.current !== nextModule) return
      const successful = qhRuns.runs.filter((item) => (
        item.status === 'success'
        && item.id > 0
        && item.module_key === qhKey
        && item.deal_id === dealId
        && item.branch
        && (snapshotId == null || item.snapshot_id === snapshotId)
      ))
      const currentIds = new Set(successful.filter((item) => item.branch === 'current').map((item) => item.id))
      const experimentIds = new Set(successful.filter((item) => item.branch === 'experiment').map((item) => item.id))
      const currentQh = successful.find((item) => item.branch === 'current') || null
      const experimentQh = successful.find((item) => item.branch === 'experiment') || null
      setQhUpstream((value) => {
        const currentId = (value.current && currentIds.has(value.current))
          ? value.current
          : (currentQh?.id || null)
        const experimentId = (value.experiment && experimentIds.has(value.experiment))
          ? value.experiment
          : (experimentQh?.id || null)
        setQhSource((prev) => ({
          current: successful.find((item) => item.id === currentId) || (keepSource ? prev.current : null),
          experiment: successful.find((item) => item.id === experimentId) || (keepSource ? prev.experiment : null),
        }))
        return { current: currentId, experiment: experimentId }
      })
    }
  }, [dealId, qhMode])

  function applyModule(next: PromptLabModuleKey) {
    const previous = moduleKeyRef.current
    bootstrapSeqRef.current += 1
    if (next.startsWith('quick_help.')) {
      setQhMode(next === 'quick_help.reanimator' ? 'reanimator' : 'push')
    }
    if (previous.startsWith('quick_help.') && next.startsWith('full_script.')) {
      const currentQh = visibleLabRun(currentRun, previous)
      const experimentQh = visibleLabRun(experimentRun, previous)
      setQhSource({ current: currentQh, experiment: experimentQh })
      setQhUpstream({
        current: currentQh && currentQh.id > 0 ? currentQh.id : null,
        experiment: experimentQh && experimentQh.id > 0 ? experimentQh.id : null,
      })
    } else if (!keepQuickHelpSource(previous, next)) {
      setQhSource({ current: null, experiment: null })
      setQhUpstream({ current: null, experiment: null })
    }
    setModuleKey(next)
    setWorkspaceLoading(true)
    setBootstrap(null)
    setCurrentPrompt('')
    setExperimentPrompt('')
    setSavedExperiment('')
    setCurrentRun(null)
    setExperimentRun(null)
    setCurrentJob(null)
    setExperimentJob(null)
    setRawExchange({ current: null, experiment: null })
    setExistingJob(null)
    setError('')
    setHistory([])
    setCurrentModel('')
    setExperimentModel('')
    setCurrentReasoning('')
    setExperimentReasoning('')
    setVersions([])
  }

  useEffect(() => {
    setWorkspaceLoading(true)
    const requestId = ++bootstrapSeqRef.current
    void loadBootstrap(moduleKey, strategyRef.current, undefined, requestId).catch((reason) => {
      if (requestId !== bootstrapSeqRef.current) return
      setWorkspaceLoading(false)
      setError(reason instanceof Error ? reason.message : String(reason))
    })
  }, [dealId, loadBootstrap, moduleKey])

  async function changeStrategy(next: ManagerQuickHelpStrategy) {
    setStrategy(next)
    if (!moduleKey.startsWith('full_script.') || workspaceLoading) return
    const requestId = bootstrapSeqRef.current
    const payload = await fetchPromptLabBootstrap(dealId, moduleKey, next, qhMode)
    if (requestId !== bootstrapSeqRef.current || moduleKeyRef.current !== moduleKey || payload.module !== moduleKey) return
    setBootstrap(payload)
    setCurrentRun(visibleLabRun(payload.production_current.lab_run || null, moduleKey))
    const useProduction = payload.production_current.exists || Boolean(payload.production_current.lab_run)
    setCurrentModel(useProduction ? payload.production_current.model : payload.runtime.model)
    setCurrentReasoning(useProduction ? payload.production_current.reasoning : payload.runtime.reasoning)
    const imported = visibleLabRun(payload.production_current.lab_run || null, moduleKey)
    if (
      imported?.upstream_run_id
      && (payload.snapshot.id == null || imported.snapshot_id === payload.snapshot.id)
    ) {
      setQhUpstream((value) => ({
        ...value,
        current: imported.upstream_run_id || value.current,
      }))
    }
  }

  useEffect(() => {
    const jobs: Array<['current' | 'experiment', PromptLabJob | null]> = [['current', currentJob], ['experiment', experimentJob]]
    const timers: number[] = []
    const activeModule = moduleKey
    for (const [branch, job] of jobs) {
      if (!job || !['queued', 'running'].includes(job.status)) continue
      if (!jobMatchesModule(job, activeModule)) continue
      const timer = window.setTimeout(async () => {
        const next = await fetchPromptLabJob(job.job_id)
        if (!jobMatchesModule(next, moduleKeyRef.current)) return
        if (branch === 'current') setCurrentJob(next)
        else setExperimentJob(next)
        const nextRun = next.run || null
        if (nextRun) {
          if (!runMatchesModule(nextRun, moduleKeyRef.current)) return
          if (branch === 'current') setCurrentRun(nextRun)
          else setExperimentRun(nextRun)
          if (moduleKeyRef.current.startsWith('quick_help.') && nextRun.id > 0) {
            setQhUpstream((value) => ({ ...value, [branch]: nextRun.id }))
            setQhSource((value) => ({ ...value, [branch]: nextRun }))
          }
        } else if (next.run_id) {
          const run = await fetchPromptLabRun(next.run_id)
          if (!runMatchesModule(run, moduleKeyRef.current)) return
          if (branch === 'current') setCurrentRun(run)
          else setExperimentRun(run)
          if (moduleKeyRef.current.startsWith('quick_help.') && run.id > 0) {
            setQhUpstream((value) => ({ ...value, [branch]: run.id }))
            setQhSource((value) => ({ ...value, [branch]: run }))
          }
        }
        if (next.status === 'done' || next.status === 'error' || next.status === 'exists') {
          if ((next.status === 'done' || next.status === 'error') && !next.reused) {
            try {
              const bundle = await fetchPromptLabRawExchange(next.job_id)
              if (moduleKeyRef.current === activeModule) {
                setRawExchange((value) => ({ ...value, [branch]: bundle }))
              }
            } catch {
              // A run can fail before reaching OpenAI, or the one-time bundle may already be consumed.
            }
          }
          const runs = await fetchPromptLabRuns({ deal_id: dealId, module_key: moduleKeyRef.current })
          if (moduleKeyRef.current !== activeModule) return
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
    applyModule(next)
  }

  async function refreshSnapshot() {
    await createPromptLabSnapshot(dealId)
    await loadBootstrap(moduleKey)
  }

  async function ensureSnapshotId() {
    if (bootstrap?.snapshot.id) return bootstrap.snapshot.id
    const created = await createPromptLabSnapshot(dealId)
    setBootstrap((current) => current ? {
      ...current,
      snapshot: {
        id: created.id,
        created_at: created.created_at,
        snapshot_hash: created.snapshot_hash,
        provenance: created.provenance,
      },
    } : current)
    return created.id
  }

  async function generate(branch: PromptLabBranch, options: { force?: boolean; silentReuse?: boolean; snapshotId?: number | null } = {}) {
    if (workspaceLoading || !bootstrap) return
    if (!bootstrap.gate.ok && family !== 'companion') {
      setError(bootstrap.gate.reason || 'Prompt Lab заблокирован')
      return
    }
    setError('')
    setRawExchange((value) => ({ ...value, [branch]: null }))
    const isCurrent = branch === 'current'
    const snapshotId = options.snapshotId ?? bootstrap?.snapshot.id ?? null
    const body = {
      deal_id: dealId,
      module_key: moduleKey,
      branch,
      snapshot_id: snapshotId,
      prompt_template: isCurrent ? currentPrompt : experimentPrompt,
      prompt_version_id: !isCurrent ? selectedVersionId : null,
      model: isCurrent ? currentModel : experimentModel,
      reasoning: isCurrent ? currentReasoning : experimentReasoning,
      question,
      selected_strategy: family === 'full_script' ? strategy : null,
      upstream_run_id: family === 'full_script' ? (isCurrent ? qhUpstream.current : qhUpstream.experiment) : null,
      quick_help_mode: family === 'full_script' ? qhMode : null,
      manager_note: family === 'companion' ? managerNote : '',
      previous_message: family === 'companion' ? previousMessage : '',
      reuse_existing: options.force ? false : options.silentReuse ? true : null,
    }
    if (family === 'full_script' && (!body.upstream_run_id || body.upstream_run_id <= 0)) {
      setError(QH_UPSTREAM_REQUIRED)
      return
    }
    const activeModule = moduleKey
    const job = await startPromptLabRun(body)
    if (!jobMatchesModule(job, moduleKeyRef.current) || moduleKeyRef.current !== activeModule) return
    if (job.status === 'exists') {
      setExistingJob(job)
      return
    }
    if (isCurrent) setCurrentJob(job)
    else setExperimentJob(job)
    if (job.run && runMatchesModule(job.run, moduleKeyRef.current)) {
      if (isCurrent) setCurrentRun(job.run)
      else setExperimentRun(job.run)
      if (moduleKeyRef.current.startsWith('quick_help.') && job.run.id > 0) {
        setQhUpstream((value) => ({ ...value, [branch]: job.run?.id || null }))
        setQhSource((value) => ({ ...value, [branch]: job.run || null }))
      }
    }
  }

  async function generateBoth() {
    const snapshotId = await ensureSnapshotId()
    await Promise.all([
      generate('current', { silentReuse: true, snapshotId }),
      generate('experiment', { force: true, snapshotId }),
    ])
  }

  function applyVersion(id: number | null) {
    setSelectedVersionId(id)
    if (!id) {
      const text = bootstrap?.production_current.prompt_template || currentPrompt
      setExperimentPrompt(text)
      setSavedExperiment(text)
      return
    }
    const version = versions.find((item) => item.id === id)
    if (!version) return
    setExperimentPrompt(version.prompt_text)
    setSavedExperiment(version.prompt_text)
  }

  function requestVersion(id: number | null) {
    if (id === selectedVersionId) return
    if (unsaved) {
      setPending({ kind: 'version', nextVersionId: id })
      return
    }
    applyVersion(id)
  }

  async function resolvePending(action: 'save' | 'continue') {
    if (!pending) return
    const next = pending
    if (action === 'save') await saveVersion()
    if (next.kind === 'leave') onConfirmLeave?.()
    else if (next.kind === 'module') applyModule(next.next)
    else applyVersion(next.nextVersionId)
    setPending(null)
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

  function downloadRawExchange(branch: PromptLabBranch, bundle: PromptLabRawExchange) {
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `prompt-lab-${moduleKey.replaceAll('.', '-')}-${branch}-run-${bundle.run_id || bundle.job_id}-raw.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  async function exportVersions(mode: 'current' | 'candidates_module' | 'candidates_all') {
    if (mode === 'current') {
      if (unsaved) {
        downloadMarkdown(`prompt-lab-${moduleKey}-draft.md`, experimentPrompt)
        return
      }
      if (!selectedVersionId) {
        downloadMarkdown(
          `prompt-lab-${moduleKey}-production.md`,
          experimentPrompt || bootstrap?.production_current.prompt_template || currentPrompt,
        )
        return
      }
    }
    const payload = await fetchPromptLabExport(mode, moduleKey, mode === 'current' ? selectedVersionId : null)
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
  const generateLocked = workspaceLoading || !bootstrap || currentBusy || experimentBusy
  const currentVisibleRun = visibleLabRun(currentRun, moduleKey)
  const experimentVisibleRun = visibleLabRun(experimentRun, moduleKey)

  return <div className="dc-prompt-lab">
    {!workspaceLoading && !bootstrap?.gate.ok && family !== 'companion' ? <p className="dc-manager-error" role="alert">{bootstrap?.gate.reason || 'Prompt Lab заблокирован'}</p> : null}
    {!workspaceLoading && bootstrap?.production_current.stale ? <p className="dc-prompt-lab-stale">Создан на предыдущем контексте</p> : null}
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
      <button type="button" className="dc-button" onClick={() => void refreshSnapshot()} disabled={workspaceLoading}>Обновить</button>
      <button type="button" className="dc-button primary" disabled={generateLocked} onClick={() => void generateBoth()}>Сгенерировать оба</button>
    </div>
    <div className={`dc-prompt-lab-grid ${layout}`}>
      {layout !== 'experiment' ? <BranchPanel
        title="CURRENT"
        readOnlyPrompt
        loading={workspaceLoading}
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
        run={currentVisibleRun}
        moduleKey={moduleKey}
        strategy={strategy}
        onStrategy={changeStrategy}
        onGenerate={() => void generate('current')}
        onCopy={onCopy}
        qhMode={qhMode}
        sourceRun={qhSource.current}
        advanced={advanced === 'current'}
        onToggleAdvanced={() => setAdvanced((value) => value === 'current' ? null : 'current')}
        rawExchange={rawExchange.current}
        onDownloadRaw={(bundle) => downloadRawExchange('current', bundle)}
      /> : null}
      {layout !== 'current' ? <BranchPanel
        title="EXPERIMENT"
        loading={workspaceLoading}
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
        run={experimentVisibleRun}
        moduleKey={moduleKey}
        strategy={strategy}
        onStrategy={changeStrategy}
        onGenerate={() => void generate('experiment')}
        onCopy={onCopy}
        qhMode={qhMode}
        sourceRun={qhSource.experiment}
        advanced={advanced === 'experiment'}
        onToggleAdvanced={() => setAdvanced((value) => value === 'experiment' ? null : 'experiment')}
        rawExchange={rawExchange.experiment}
        onDownloadRaw={(bundle) => downloadRawExchange('experiment', bundle)}
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
        const raw = event.target.value
        requestVersion(raw ? Number(raw) : null)
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
    {currentVisibleRun && currentVisibleRun.id > 0 && experimentVisibleRun && experimentVisibleRun.id > 0 ? <div className="dc-prompt-lab-review">
      <span>A/B</span>
      {([['current_better', 'CURRENT лучше'], ['same', 'Одинаково'], ['experiment_better', 'EXPERIMENT лучше'], ['both_bad', 'Оба плохие']] as const).map(([verdict, label]) => (
        <button key={verdict} type="button" onClick={() => void savePromptLabReview({ current_run_id: currentVisibleRun.id, experiment_run_id: experimentVisibleRun.id, prompt_version_id: selectedVersionId, verdict })}>{label}</button>
      ))}
    </div> : null}
    {history.length ? <details className="dc-prompt-lab-history"><summary>История runs</summary>
      <table><thead><tr><th>Время МСК</th><th>Ветка</th><th>Модель</th><th>Reasoning</th><th>Статус</th><th>ms</th><th>₽</th></tr></thead>
        <tbody>{history.map((item) => <tr key={item.id}><td>{moscowStamp(item.created_at)}</td><td>{item.branch}</td><td>{item.model}</td><td>{item.reasoning}</td><td>{item.status === 'success' ? '✓ Готово' : `✕ ${item.error || 'ошибка'}`}</td><td>{item.latency_seconds ? Math.round(item.latency_seconds * 1000) : '—'}</td><td>{String((item.cost as { estimated_cost_rub?: number } | null)?.estimated_cost_rub ?? '—')}</td></tr>)}</tbody>
      </table>
    </details> : null}
    {pending ? <div className="dc-prompt-lab-modal" role="dialog">
      <p>Есть несохранённые изменения.</p>
      <button type="button" className="dc-button primary" onClick={() => void resolvePending('save')}>Сохранить новую версию</button>
      <button type="button" className="dc-button" onClick={() => void resolvePending('continue')}>Продолжить без сохранения</button>
      <button type="button" onClick={() => setPending(null)}>Отмена</button>
    </div> : null}
    {existingJob ? <div className="dc-prompt-lab-modal" role="dialog">
      <p>Такая конфигурация уже запускалась.</p>
      <button type="button" className="dc-button primary" onClick={() => {
        if (existingJob.run && jobMatchesModule(existingJob, moduleKey) && runMatchesModule(existingJob.run, moduleKey) && existingJob.branch === 'current') setCurrentRun(existingJob.run)
        if (existingJob.run && jobMatchesModule(existingJob, moduleKey) && runMatchesModule(existingJob.run, moduleKey) && existingJob.branch === 'experiment') setExperimentRun(existingJob.run)
        setExistingJob(null)
      }}>Использовать результат</button>
      <button type="button" className="dc-button" onClick={() => { const branch = existingJob.branch; setExistingJob(null); void generate(branch, { force: true }) }}>Запустить заново</button>
    </div> : null}
  </div>
}

function MaterialSourceBar({
  mode,
  sourceRun,
  strategy,
  onStrategy,
  loading,
}: {
  mode: ManagerAssistantMode
  sourceRun: PromptLabRun | null
  strategy: ManagerQuickHelpStrategy
  onStrategy: (value: ManagerQuickHelpStrategy) => void
  loading?: boolean
}) {
  const result = sourceRun?.result
  const caption = materialSourceCaption(mode, strategyLabelFromResult(result, strategy))
  const preview = selectedClientMessage(result, strategy)
  return <section className="dc-prompt-lab-source" aria-label="Исход Quick Help">
    <strong>{caption}</strong>
    <div className="dc-manager-tone-tabs labeled" role="tablist" aria-label="Вариант Quick Help">
      {SOURCE_STRATEGIES.map((item) => (
        <button
          key={item}
          type="button"
          role="tab"
          aria-selected={strategy === item}
          className={strategy === item ? 'active' : ''}
          disabled={loading}
          onClick={() => onStrategy(item)}
        >
          <span>{strategyLabelFromResult(result, item)}</span>
        </button>
      ))}
    </div>
    <pre>{preview || (loading ? PROMPT_LAB_LOADING_TEXT : QH_UPSTREAM_REQUIRED)}</pre>
  </section>
}

function BranchPanel(props: {
  title: string
  readOnlyPrompt?: boolean
  loading?: boolean
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
  qhMode: ManagerAssistantMode
  sourceRun: PromptLabRun | null
  advanced: boolean
  onToggleAdvanced: () => void
  rawExchange: PromptLabRawExchange | null
  onDownloadRaw: (bundle: PromptLabRawExchange) => void
  extraActions?: React.ReactNode
}) {
  const mode = props.moduleKey.startsWith('quick_help.')
    ? (props.moduleKey === 'quick_help.push' ? 'push' : 'reanimator')
    : props.qhMode
  const visibleRun = visibleLabRun(props.run, props.moduleKey)
  return <section className="dc-prompt-lab-branch">
    <header>
      <strong>{props.title}</strong>
      <span>{props.loading ? PROMPT_LAB_LOADING_TEXT : props.job?.status === 'error' ? `✕ ${props.job.error || props.job.detail}` : props.busy ? props.job?.detail || 'Генерация…' : visibleRun?.status === 'success' ? '✓ Готово' : visibleRun?.status === 'error' ? `✕ ${visibleRun.error}` : 'Нет результата'}</span>
    </header>
    {props.moduleKey.startsWith('full_script.') ? <MaterialSourceBar
      mode={mode}
      sourceRun={props.sourceRun}
      strategy={props.strategy}
      onStrategy={props.onStrategy}
      loading={props.loading}
    /> : null}
    <label>Prompt
      <textarea readOnly={props.readOnlyPrompt || props.loading} value={props.loading ? PROMPT_LAB_LOADING_TEXT : props.prompt} onChange={(event) => props.onPrompt(event.target.value)} />
    </label>
    <div className="dc-prompt-lab-config">
      <label>Model
        <select value={props.model} disabled={props.loading} onChange={(event) => {
          props.onModel(event.target.value)
          const allowed = props.models.find((item) => item.id === event.target.value)?.reasoning || []
          if (!allowed.includes(props.reasoning)) props.onReasoning(allowed[0] || 'low')
        }}>
          {props.models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
      </label>
      <label>Reasoning
        <select value={props.reasoning} disabled={props.loading} onChange={(event) => props.onReasoning(event.target.value)}>
          {props.reasoningOptions.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <button type="button" className="dc-button primary" disabled={props.busy || props.loading} onClick={props.onGenerate}>Сгенерировать</button>
    </div>
    {props.extraActions}
    <LabResult run={visibleRun} moduleKey={props.moduleKey} mode={mode} strategy={props.strategy} onStrategy={props.onStrategy} onCopy={props.onCopy} />
    {props.rawExchange ? <button type="button" className="dc-link-button" onClick={() => props.onDownloadRaw(props.rawExchange as PromptLabRawExchange)}>Скачать сырой обмен</button> : null}
    <button type="button" className="dc-link-button" onClick={props.onToggleAdvanced}>{props.advanced ? 'Скрыть Advanced' : 'Advanced'}</button>
    {props.advanced && visibleRun ? <pre className="dc-prompt-lab-advanced">{JSON.stringify({
      effective_prompt: visibleRun.effective_prompt,
      prompt_hash: visibleRun.prompt_hash,
      model: visibleRun.model,
      reasoning: visibleRun.reasoning,
      schema_version: visibleRun.schema_version,
      usage: visibleRun.usage,
      cost: visibleRun.cost,
      latency_seconds: visibleRun.latency_seconds,
      response_status: visibleRun.response_status,
      error: visibleRun.error,
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
  const visible = visibleLabRun(run, moduleKey)
  const kind = labResultKind(visible, moduleKey)
  if (!visible || kind === 'empty') return <p className="empty">Результат ещё не сформирован</p>
  if (kind === 'error') return <p className="dc-manager-error">{visible.error || 'Ошибка генерации'}</p>
  const result = visible.result
  if (!result) return <p className="empty">Результат ещё не сформирован</p>
  if (kind === 'quick_help') {
    return <QuickHelpResultView
      entry={{ content: result as unknown as ManagerQuickHelpContent, created_at: visible.created_at }}
      mode={mode}
      selectedStrategy={strategy}
      onSelectedStrategy={onStrategy}
      onCopy={onCopy}
    />
  }
  if (kind === 'followups') {
    return <FollowupsResultView record={{ id: visible.id, created_at: visible.created_at, content: result as unknown as ManagerFollowupsRecord['content'] }} />
  }
  if (kind === 'companion') {
    return <CompanionResultView companion={{ id: visible.id, created_at: visible.created_at, content: result as unknown as ManagerCompanionRecord['content'] } as ManagerCompanionRecord} onCopy={(text) => void onCopy(text, 'Сопроводительный текст')} />
  }
  return <FullScriptResultView script={result as unknown as ManagerFullScriptContent} onCopy={onCopy} />
}
