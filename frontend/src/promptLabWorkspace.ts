export const PROMPT_LAB_LOADING_TEXT = 'Загружаем prompt…'

export type PromptLabModuleRef = string

export type PromptLabRunRef = {
  module_key?: string | null
  status?: string | null
  result?: unknown
}

export type PromptLabJobRef = {
  module_key?: string | null
  job_id?: string
}

export type PromptLabWorkspaceSlice = {
  moduleKey: PromptLabModuleRef
  loading: boolean
  requestId: number
  currentPrompt: string
  experimentPrompt: string
  savedExperiment: string
  currentRun: PromptLabRunRef | null
  experimentRun: PromptLabRunRef | null
  currentJob: PromptLabJobRef | null
  experimentJob: PromptLabJobRef | null
}

export type LabResultKind = 'empty' | 'error' | 'quick_help' | 'followups' | 'companion' | 'full_script'

export function runMatchesModule(run: PromptLabRunRef | null | undefined, moduleKey: PromptLabModuleRef): boolean {
  return Boolean(run && String(run.module_key || '') === String(moduleKey))
}

export function visibleLabRun<T extends PromptLabRunRef>(run: T | null | undefined, moduleKey: PromptLabModuleRef): T | null {
  return runMatchesModule(run, moduleKey) ? (run as T) : null
}

export function jobMatchesModule(job: PromptLabJobRef | null | undefined, moduleKey: PromptLabModuleRef): boolean {
  return Boolean(job && String(job.module_key || '') === String(moduleKey))
}

export function isCurrentRequest(requestId: number, latestId: number): boolean {
  return requestId === latestId
}

export function assistantModeLabel(mode: 'push' | 'reanimator'): string {
  return mode === 'push' ? 'Дожим' : 'Реаниматор'
}

export function materialSourceCaption(mode: 'push' | 'reanimator', strategyLabelText: string): string {
  return `${assistantModeLabel(mode)} · ${strategyLabelText}`
}

export function selectedClientMessage(result: unknown, strategy: string): string {
  if (!result || typeof result !== 'object') return ''
  const messages = (result as { client_messages?: Record<string, unknown> }).client_messages
  if (!messages || typeof messages !== 'object') return ''
  return String(messages[strategy] || '').trim()
}

export function strategyLabelFromResult(result: unknown, strategy: string): string {
  const fallbacks: Record<string, string> = {
    primary: 'Основной ход',
    alternative: 'Другой заход',
    pattern_break: 'Смена механики',
  }
  const fallback = fallbacks[strategy] || strategy
  if (!result || typeof result !== 'object') return fallback
  const labels = (result as { strategy_labels?: Record<string, unknown> }).strategy_labels
  if (!labels || typeof labels !== 'object') return fallback
  const label = String(labels[strategy] ?? '').trim()
  return label || fallback
}

export function keepQuickHelpSource(previousModule: PromptLabModuleRef, nextModule: PromptLabModuleRef): boolean {
  const fromQuickHelp = previousModule.startsWith('quick_help.')
  const fromMaterials = previousModule.startsWith('full_script.')
  const toMaterials = nextModule.startsWith('full_script.')
  const sameQuickHelp = fromQuickHelp && nextModule === previousModule
  return Boolean((fromQuickHelp && toMaterials) || (fromMaterials && toMaterials) || sameQuickHelp)
}

export function labResultKind(run: PromptLabRunRef | null | undefined, moduleKey: PromptLabModuleRef): LabResultKind {
  const visible = visibleLabRun(run, moduleKey)
  if (!visible) return 'empty'
  if (visible.status === 'error') return 'error'
  if (!visible.result) return 'empty'
  if (moduleKey.startsWith('quick_help.')) return 'quick_help'
  if (moduleKey === 'followups') return 'followups'
  if (moduleKey === 'companion') return 'companion'
  return 'full_script'
}

export function beginModuleLoad(state: PromptLabWorkspaceSlice, nextModule: PromptLabModuleRef): PromptLabWorkspaceSlice {
  return {
    moduleKey: nextModule,
    loading: true,
    requestId: state.requestId + 1,
    currentPrompt: '',
    experimentPrompt: '',
    savedExperiment: '',
    currentRun: null,
    experimentRun: null,
    currentJob: null,
    experimentJob: null,
  }
}

export function applyBootstrapIfCurrent(
  state: PromptLabWorkspaceSlice,
  requestId: number,
  moduleKey: PromptLabModuleRef,
  payload: { module?: string | null; prompt?: string; imported?: PromptLabRunRef | null },
): PromptLabWorkspaceSlice | null {
  if (!isCurrentRequest(requestId, state.requestId)) return null
  if (state.moduleKey !== moduleKey) return null
  if (payload.module && payload.module !== moduleKey) return null
  const prompt = String(payload.prompt || '')
  return {
    ...state,
    loading: false,
    currentPrompt: prompt,
    experimentPrompt: prompt,
    savedExperiment: prompt,
    currentRun: visibleLabRun(payload.imported || null, moduleKey),
    experimentRun: null,
  }
}

export function applyJobIfCurrent(
  state: PromptLabWorkspaceSlice,
  job: PromptLabJobRef | null | undefined,
  run: PromptLabRunRef | null | undefined,
  branch: 'current' | 'experiment',
): PromptLabWorkspaceSlice | null {
  if (state.loading) return null
  if (!jobMatchesModule(job, state.moduleKey)) return null
  if (run && !runMatchesModule(run, state.moduleKey)) return null
  if (branch === 'current') {
    return { ...state, currentJob: job || null, currentRun: run || state.currentRun }
  }
  return { ...state, experimentJob: job || null, experimentRun: run || state.experimentRun }
}
