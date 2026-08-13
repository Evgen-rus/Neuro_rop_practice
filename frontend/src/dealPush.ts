import type {
  ManagerQuickHelpContent,
  ManagerQuickHelpEntry,
  ManagerQuickHelpStrategy,
  ManagerQuickHelpStrategyV3Content,
} from './api'

export type AssistantMode = 'push' | 'reanimator'

export const STRATEGY_FALLBACK_LABELS: Record<ManagerQuickHelpStrategy, string> = {
  primary: 'Основной ход',
  alternative: 'Другой заход',
  pattern_break: 'Смена механики',
}

export function isStrategyV3(
  content: ManagerQuickHelpContent,
): content is ManagerQuickHelpStrategyV3Content {
  return content.answer_contract === 'strategy_v3'
}

export function entryMode(entry: Pick<ManagerQuickHelpEntry, 'mode' | 'content'>): AssistantMode {
  if (entry.mode === 'push' || entry.mode === 'reanimator') return entry.mode
  if (isStrategyV3(entry.content) && (entry.content.mode === 'push' || entry.content.mode === 'reanimator')) {
    return entry.content.mode
  }
  return 'reanimator'
}

export function isAutoOrigin(entry: Pick<ManagerQuickHelpEntry, 'origin'>): boolean {
  return entry.origin === 'auto'
}

export function strategyLabel(
  content: ManagerQuickHelpContent,
  strategy: ManagerQuickHelpStrategy,
): string {
  if (isStrategyV3(content)) {
    const label = content.strategy_labels[strategy]?.trim()
    if (label) return label
  }
  return STRATEGY_FALLBACK_LABELS[strategy]
}

export function pressureLever(content: ManagerQuickHelpContent): { title: string; rationale: string } | null {
  if (!isStrategyV3(content)) return null
  const title = content.pressure_lever.title.trim()
  const rationale = content.pressure_lever.rationale.trim()
  if (!title || !rationale) return null
  return { title, rationale }
}

export function currentEntryForMode(
  entries: ManagerQuickHelpEntry[],
  mode: AssistantMode,
  sourceReportId?: number | null,
  situationReviewId?: number | null,
): ManagerQuickHelpEntry | null {
  const ranked = [...entries].sort((first, second) => second.id - first.id)
  return ranked.find((entry) => {
    if (entryMode(entry) !== mode) return false
    if (sourceReportId != null && Number(entry.source_report_id) !== Number(sourceReportId)) return false
    if (situationReviewId != null && Number(entry.situation_review_id) !== Number(situationReviewId)) return false
    return true
  }) || null
}

export function entriesForMode(entries: ManagerQuickHelpEntry[], mode: AssistantMode): ManagerQuickHelpEntry[] {
  return [...entries].filter((entry) => entryMode(entry) === mode).sort((first, second) => first.id - second.id)
}

export function missingCurrentModes(
  currentByMode: Partial<Record<AssistantMode, ManagerQuickHelpEntry | null>> | null | undefined,
): AssistantMode[] {
  const missing: AssistantMode[] = []
  if (!currentByMode?.push) missing.push('push')
  if (!currentByMode?.reanimator) missing.push('reanimator')
  return missing
}

export function visibleLifehack<T>(lifehacks: T[], index: number): { item: T; index: number; total: number } | null {
  if (!lifehacks.length) return null
  const safeIndex = Math.min(Math.max(0, index), lifehacks.length - 1)
  return { item: lifehacks[safeIndex], index: safeIndex, total: lifehacks.length }
}

export function answerModeClassName(mode: AssistantMode): string {
  return mode === 'push' ? 'dc-manager-answer mode-push' : 'dc-manager-answer mode-reanimator'
}

export function workspaceModeClassName(mode: AssistantMode): string {
  return mode === 'push' ? 'dc-manager-assistant-modal mode-push' : 'dc-manager-assistant-modal mode-reanimator'
}
