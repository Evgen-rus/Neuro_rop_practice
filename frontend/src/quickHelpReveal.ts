/** Progressive reveal for a ready Quick Help answer. This is a frontend-only UX, not LLM streaming. */

export const QUICK_HELP_SUMMARY_TYPE_MS = 600
export const QUICK_HELP_SUMMARY_HOLD_MS = 150
export const QUICK_HELP_CARD_STAGGER_MS = 250
export const QUICK_HELP_REVEAL_BUDGET_MS = 1400

export type QuickHelpRevealStep = 'summary' | 'message' | 'secondary' | 'fallback' | 'done'

export function prefersReducedMotion(media?: { matches: boolean } | null): boolean {
  return Boolean(media?.matches)
}

export function fragmentText(text: string, maxFragments = 8): string[] {
  if (!text) return []
  const words = text.match(/\S+\s*/g)
  if (!words) return [text]
  if (words.length <= maxFragments) return words
  const size = Math.ceil(words.length / maxFragments)
  const fragments: string[] = []
  for (let index = 0; index < words.length; index += size) {
    fragments.push(words.slice(index, index + size).join(''))
  }
  return fragments
}

export function typedText(fragments: string[], visibleCount: number): string {
  if (visibleCount <= 0) return ''
  if (visibleCount >= fragments.length) return fragments.join('')
  return fragments.slice(0, visibleCount).join('')
}

export function summaryTypingDone(elapsedMs: number): boolean {
  return elapsedMs >= QUICK_HELP_SUMMARY_TYPE_MS
}

export function revealStepAt(elapsedMs: number): QuickHelpRevealStep {
  const messageAt = QUICK_HELP_SUMMARY_TYPE_MS + QUICK_HELP_SUMMARY_HOLD_MS
  if (elapsedMs < messageAt) return 'summary'
  if (elapsedMs < messageAt + QUICK_HELP_CARD_STAGGER_MS) return 'message'
  if (elapsedMs < messageAt + QUICK_HELP_CARD_STAGGER_MS * 2) return 'secondary'
  if (elapsedMs < QUICK_HELP_REVEAL_BUDGET_MS) return 'fallback'
  return 'done'
}

export function shouldAnimateQuickHelpAnswer(input: {
  entryId: number
  freshEntryId: number | null
  viewingLatest: boolean
  reducedMotion: boolean
}): boolean {
  return (
    !input.reducedMotion
    && input.viewingLatest
    && input.freshEntryId != null
    && input.entryId === input.freshEntryId
  )
}

export function freshQuickHelpIdFromJob(job: {
  quick_help_id?: number | null
  entry_id?: number | null
  entry?: { id?: number | null } | null
}): number | null {
  const value = job.quick_help_id ?? job.entry_id ?? job.entry?.id
  return typeof value === 'number' && Number.isFinite(value) && value >= 1 ? value : null
}

export function latestQuickHelpEntryId(entries: Array<{ id: number }>): number | null {
  if (!entries.length) return null
  return entries.reduce((max, entry) => Math.max(max, entry.id), entries[0].id)
}

export function revealClassName(base: string, animate: boolean): string {
  return animate ? `${base} dc-manager-answer-reveal` : base
}
