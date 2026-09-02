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

export type AutomaticAnalysisDetail = {
  deal_id: string
  title: string
  decision: 'full' | 'mini'
  incremental: boolean
  reasons: string[]
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
  updated_deal_ids?: string[]
  current_stage: string | null
  current?: AutomaticAnalysisCurrent | null
  started_at: string | null
  updated_at: string | null
  finished_at: string | null
  details?: AutomaticAnalysisDetail[]
}

export type AutomaticAnalysisRefreshPlan = {
  reloadPortfolio: boolean
  dealIds: string[]
}

export function canViewAutomaticAnalysis(role: string): boolean {
  return role === 'admin'
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
  'status' | 'started_at' | 'updated_deal_ids'
>

function normalizedDealIds(value: string[] | null | undefined): string[] {
  const ids: string[] = []
  const seen = new Set<string>()
  for (const raw of value || []) {
    const id = String(raw || '').trim()
    if (!id || seen.has(id)) continue
    seen.add(id)
    ids.push(id)
  }
  return ids
}

export function automaticAnalysisRefreshPlan(
  previous: AutomaticAnalysisRefreshSnapshot | null | undefined,
  next: AutomaticAnalysisRefreshSnapshot | null | undefined,
): AutomaticAnalysisRefreshPlan {
  const empty: AutomaticAnalysisRefreshPlan = { reloadPortfolio: false, dealIds: [] }
  if (!previous || !next) return empty

  const previousStartedAt = String(previous.started_at || '')
  const nextStartedAt = String(next.started_at || '')
  const runChanged = Boolean(
    previousStartedAt
    && nextStartedAt
    && previousStartedAt !== nextStartedAt,
  )

  // A new packet appears only after Bitrix sync. One full dashboard reload
  // picks up CRM fields and tasks across the visible list.
  if (runChanged) {
    return { reloadPortfolio: true, dealIds: [] }
  }

  const previousIds = new Set(normalizedDealIds(previous.updated_deal_ids))
  const dealIds = normalizedDealIds(next.updated_deal_ids).filter((id) => !previousIds.has(id))
  return { reloadPortfolio: false, dealIds }
}
