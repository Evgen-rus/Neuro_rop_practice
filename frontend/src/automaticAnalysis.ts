export type AutomaticAnalysisStatus =
  | 'running'
  | 'done'
  | 'error'
  | 'interrupted'
  | 'skipped_empty'
  | 'skipped_busy'
  | 'skipped_unconfigured'
  | 'skipped_locked'
  | string

export type AutomaticAnalysisCurrent = {
  title: string
  stage: string | null
}

export type AutomaticAnalysisLatest = {
  business_date: string | null
  status: AutomaticAnalysisStatus
  processed: number
  total: number
  succeeded: number
  errors: number
  skipped: number
  full: number
  mini: number
  reports_published: number
  current_stage: string | null
  current?: AutomaticAnalysisCurrent | null
  started_at: string | null
  updated_at: string | null
  finished_at: string | null
}

export const AUTOMATIC_ANALYSIS_RUNNING_POLL_MS = 2500
export const AUTOMATIC_ANALYSIS_IDLE_POLL_MS = 15000

const STATUS_LABELS: Record<string, string> = {
  running: 'Идёт автоматический анализ',
  done: 'Автоматический пакет завершён',
  error: 'Автоматический пакет завершился с ошибкой',
  interrupted: 'Автоматический пакет прерван',
  skipped_empty: 'Нет сделок для автоматического анализа',
  skipped_busy: 'Автоматический анализ пропущен: сделки уже в работе',
  skipped_unconfigured: 'Автоматический анализ пропущен: выборка не настроена',
  skipped_locked: 'Автоматический анализ уже выполняется',
}

const STAGE_LABELS: Record<string, string> = {
  queued: 'В очереди',
  crm_context: 'Сбор CRM',
  audio_download: 'Загрузка звонков',
  transcription: 'Транскрибация',
  llm_analysis: 'Анализ',
  validation: 'Проверка ответа',
  report: 'Формирование отчёта',
  done: 'Готово',
  error: 'Ошибка',
}

export function automaticAnalysisPollInterval(status: string | null | undefined): number {
  return status === 'running' ? AUTOMATIC_ANALYSIS_RUNNING_POLL_MS : AUTOMATIC_ANALYSIS_IDLE_POLL_MS
}

export function automaticAnalysisStatusLabel(status: string | null | undefined): string {
  const key = String(status || '').trim()
  if (!key) return 'Автоматический анализ ещё не запускался'
  return STATUS_LABELS[key] || `Статус: ${key}`
}

export function automaticAnalysisStageLabel(stage: string | null | undefined): string | null {
  const key = String(stage || '').trim()
  if (!key) return null
  return STAGE_LABELS[key] || key
}

export function automaticAnalysisCurrentText(
  snapshot: Pick<AutomaticAnalysisLatest, 'status' | 'current' | 'current_stage'>,
): string | null {
  if (snapshot.status !== 'running') return null
  const title = String(snapshot.current?.title || '').trim()
  if (!title) return null
  const stage = automaticAnalysisStageLabel(snapshot.current?.stage || snapshot.current_stage)
  return stage ? `сейчас: ${title} · ${stage}` : `сейчас: ${title}`
}

export function automaticAnalysisCountersText(snapshot: Pick<
  AutomaticAnalysisLatest,
  'processed' | 'total' | 'full' | 'mini' | 'skipped' | 'errors' | 'reports_published'
>): string {
  return [
    `обработано ${snapshot.processed} из ${snapshot.total}`,
    `FULL ${snapshot.full}`,
    `MINI ${snapshot.mini}`,
    `skip ${snapshot.skipped}`,
    `ошибок ${snapshot.errors}`,
    `новых отчётов ${snapshot.reports_published}`,
  ].join(' · ')
}

export function shouldReloadAfterReportsPublished(
  previous: number | null | undefined,
  next: number | null | undefined,
): boolean {
  const before = Number(previous || 0)
  const after = Number(next || 0)
  return after > before
}

type AutomaticAnalysisRefreshSnapshot = Pick<
  AutomaticAnalysisLatest,
  'status' | 'started_at' | 'reports_published'
>

export function shouldReloadAfterAutomaticAnalysis(
  previous: AutomaticAnalysisRefreshSnapshot | null | undefined,
  next: AutomaticAnalysisRefreshSnapshot | null | undefined,
): boolean {
  if (!previous || !next) return false

  const previousStartedAt = String(previous.started_at || '')
  const nextStartedAt = String(next.started_at || '')
  const runChanged = Boolean(
    previousStartedAt
    && nextStartedAt
    && previousStartedAt !== nextStartedAt,
  )
  const nextTerminal = next.status !== 'running'

  if (runChanged && nextTerminal) return true
  if (!runChanged && previous.status === 'running' && nextTerminal) return true
  return shouldReloadAfterReportsPublished(previous.reports_published, next.reports_published)
}
