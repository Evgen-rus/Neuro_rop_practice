export type Priority = 'high' | 'medium' | 'low'

export type Candidate = {
  entity_type: 'lead' | 'deal'
  entity_id: string
  pipeline_id?: string
  title: string
  client_name: string
  status: string
  stage_id?: string
  amount?: string
  manager_id?: string
  date_modify?: string
  date_create?: string
  stale_days?: number | null
  priority: Priority
  score: number
  attention_reason: string
  reasons: string[]
  closed_reason_type?: string | null
  bitrix_url?: string
  analyzed?: boolean
  converted_handoff?: boolean
  review_state?: 'reviewed' | 'snoozed' | 'changed'
  review_change_reason?: string
  review_decision?: string
  reviewed_at?: string
  crm_updated_after_review?: boolean
  journey_key?: string
  origin_lead_id?: string | null
  reason_codes?: string[]
  analysis_freshness?: 'fresh' | 'changed' | 'date_modified_only' | 'missing' | 'failed' | 'reviewed' | 'snoozed'
  lifecycle?: 'new' | 'backlog' | 'reactivation'
  workset_selected?: boolean
  capacity_state?: 'waiting_for_capacity'
  call_method?: Record<string, unknown>
}

export type AnalysisProfileSettings = {
  timezone: string
  period_preset: 'today' | 'yesterday' | 'today_and_yesterday'
  lead: Record<string, unknown>
  deal: Record<string, unknown>
  signals: Record<string, boolean>
  review_view: 'active' | 'reviewed' | 'all'
  limits: {
    workset: number
    new_slots: number
    backlog_slots: number
    paid_per_run: number
    paid_per_day: number
  }
  analysis: Record<string, unknown>
}

export type AnalysisProfile = {
  id: number
  name: string
  version: number
  profile: AnalysisProfileSettings
  created_at: string
  updated_at: string
}

export type DailyPreview = {
  profile: { id: number; name: string; version: number }
  period: Record<string, string>
  scope: Record<string, unknown>
  summary: Record<string, number>
  cost_preview: Record<string, unknown>
  candidates: Candidate[]
  generated_at: string
  llm_called: false
}

export type DailySummaryRun = {
  id: number
  profile_id: number
  profile_name: string
  profile_version: number
  profile_snapshot: AnalysisProfileSettings
  period: Record<string, string>
  scope: Record<string, unknown>
  cost_preview: Record<string, unknown>
  status: string
  selected_count: number
  llm_required_count: number
  llm_allowed_count: number
  job_id?: string | null
  created_at: string
  items?: Array<Record<string, unknown>>
  job_states?: JobState[]
  results?: JobResult[]
}

export type CandidatesResponse = {
  created_days?: number
  modified_days?: number
  days: number
  limit: number
  entity_type: string
  pipeline_ids?: string[]
  stage_ids?: string[]
  review_view?: 'active' | 'reviewed' | 'all'
  ready?: boolean
  ready_message?: string
  generated_at: string
  summary: {
    total_scored: number
    returned: number
    high: number
    medium: number
    low: number
    already_analyzed: number
    reviewed_hidden?: number
    reviewed_visible?: number
    changed_after_review?: number
    crm_updated_after_review?: number
  }
  candidates: Candidate[]
}

export type PipelineStage = {
  id: string
  name: string
}

export type CrmPipeline = {
  id: string
  name: string
  stages: PipelineStage[]
}

export type PipelinesResponse = {
  deal_pipelines: CrmPipeline[]
  lead_pipeline: CrmPipeline
}

export type CandidateFilter = {
  entity_type: 'lead' | 'deal'
  created_days: number
  modified_days: number
  limit: number
  priority: Priority | null
  pipeline_ids: string[]
  stage_ids: string[]
  review_view: 'active' | 'reviewed' | 'all'
}

export type AnalyzeOptions = {
  entity_type: 'lead' | 'deal' | 'auto'
  ids: string
  history_days: number
  include_related: boolean
  include_internal: boolean
  download_audio: boolean
  redownload_audio: boolean
  transcribe_audio: boolean
  analyze: boolean
  force_llm: boolean
  confirm_paid?: boolean
  transcript_mode: 'all' | 'latest' | 'none'
}

export type JobStage = {
  key: string
  label: string
  status: string
  detail?: string
  updated_at?: string
}

export type JobResult = {
  entity_type: string
  entity_id: string
  report_id?: number | null
  has_analysis: boolean
  has_markdown: boolean
  risk_level?: string | null
  attention_reason?: string | null
  recommended_action?: string | null
  bitrix_url?: string | null
  analysis?: Record<string, unknown> | null
}

export type JobState = {
  job_id: string
  status: string
  created_at: string
  updated_at: string
  options: Record<string, unknown>
  stages: JobStage[]
  current_stage?: string | null
  results: JobResult[]
  report_ids: number[]
  logs: string[]
  error?: string | null
}

export type UiReportListItem = {
  id: number
  entity_type: string
  entity_id: string
  created_at: string
  risk_level?: string | null
  attention_reason?: string | null
  recommended_action?: string | null
  analysis_path?: string | null
  report_path?: string | null
  job_id?: string | null
  bitrix_url?: string | null
}

export type UiReportDetail = UiReportListItem & {
  report_json?: Record<string, unknown> | null
  report_markdown?: string | null
  decisions?: Array<Record<string, unknown>>
  outcomes?: Array<Record<string, unknown>>
  candidate_review?: Record<string, unknown> | null
}

export type CompactRun = {
  id: string
  entity_type: 'lead' | 'deal'
  entity_id: string
  snapshot_hash: string
  status: string
  started_at: string
  completed_at?: string | null
  model?: string | null
  analysis?: Record<string, unknown> | null
  evidence_coverage?: Record<string, unknown>
  fallback_class?: string | null
  usage?: Record<string, unknown>
  cost_rub?: number | null
  is_current: boolean
  feedback?: Record<string, unknown> | null
}

export type CompactReview = {
  entity_type: 'lead' | 'deal'
  entity_id: string
  full_analysis?: Record<string, unknown> | null
  snapshot_hash?: string | null
  preflight_error?: string | null
  selected_run?: CompactRun | null
  runs: CompactRun[]
}

export type CompactJob = {
  job_id: string
  entity_type: 'lead' | 'deal'
  entity_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  run_id?: string | null
  error?: string | null
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
    ...init,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const payload = await response.json()
      detail = payload.detail || JSON.stringify(payload)
    } catch {
      // ignore
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export function fetchPipelines() {
  return api<PipelinesResponse>('/api/pipelines')
}

export function fetchCandidateFilter() {
  return api<{ filter: CandidateFilter }>('/api/candidate-filters')
}

export function saveCandidateFilter(body: CandidateFilter) {
  return api<{ ok: boolean; filter: CandidateFilter }>('/api/candidate-filters', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function fetchAnalysisProfiles() {
  return api<{ items: AnalysisProfile[]; selected: AnalysisProfile }>('/api/analysis-profiles')
}

export function createAnalysisProfile(name: string, profile: AnalysisProfileSettings) {
  return api<{ ok: boolean; profile: AnalysisProfile }>('/api/analysis-profiles', {
    method: 'POST',
    body: JSON.stringify({ name, profile }),
  })
}

export function updateAnalysisProfile(profile: AnalysisProfile) {
  return api<{ ok: boolean; profile: AnalysisProfile }>(`/api/analysis-profiles/${profile.id}`, {
    method: 'PUT',
    body: JSON.stringify({ name: profile.name, profile: profile.profile }),
  })
}

export function deleteAnalysisProfile(profileId: number) {
  return api<{ ok: boolean; selected: AnalysisProfile; items: AnalysisProfile[] }>(`/api/analysis-profiles/${profileId}`, {
    method: 'DELETE',
  })
}

export function selectAnalysisProfile(profileId: number) {
  return api<{ ok: boolean; selected: AnalysisProfile }>(`/api/analysis-profiles/${profileId}/selected`, { method: 'PUT' })
}

export function previewAnalysisProfile(profileId: number) {
  return api<DailyPreview>(`/api/analysis-profiles/${profileId}/preview`, { method: 'POST' })
}

export function createDailySummary(
  profile: AnalysisProfile,
  preview: DailyPreview,
  selectedJourneyKeys: string[],
) {
  return api<DailySummaryRun>('/api/daily-summaries', {
    method: 'POST',
    body: JSON.stringify({
      profile_id: profile.id,
      profile_version: profile.version,
      preview,
      selected_journey_keys: selectedJourneyKeys,
    }),
  })
}

export function fetchDailySummaries(limit = 30) {
  return api<{ items: DailySummaryRun[] }>(`/api/daily-summaries?limit=${limit}`)
}

export function fetchDailySummary(runId: number) {
  return api<DailySummaryRun>(`/api/daily-summaries/${runId}`)
}

export function startDailySummary(runId: number, confirmPaid: boolean) {
  return api<{ summary: DailySummaryRun; jobs: JobState[] }>(`/api/daily-summaries/${runId}/start`, {
    method: 'POST',
    body: JSON.stringify({ confirm_paid: confirmPaid }),
  })
}

export function fetchCandidates(params: {
  entity_type?: 'lead' | 'deal'
  created_days?: number
  modified_days?: number
  days?: number
  limit?: number
  priority?: string
  pipeline_ids?: string[]
  stage_ids?: string[]
  review_view?: 'active' | 'reviewed' | 'all'
}) {
  const query = new URLSearchParams()
  if (params.entity_type) query.set('entity_type', params.entity_type)
  if (params.created_days !== undefined) query.set('created_days', String(params.created_days))
  if (params.modified_days !== undefined) query.set('modified_days', String(params.modified_days))
  if (params.days !== undefined) query.set('days', String(params.days))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  if (params.priority) query.set('priority', params.priority)
  for (const id of params.pipeline_ids || []) query.append('pipeline_ids', id)
  for (const id of params.stage_ids || []) query.append('stage_ids', id)
  if (params.review_view) query.set('review_view', params.review_view)
  return api<CandidatesResponse>(`/api/candidates?${query.toString()}`)
}

export function searchCandidates(body: {
  entity_type: 'lead' | 'deal'
  created_days: number
  modified_days: number
  limit?: number
  priority?: string | null
  pipeline_ids: string[]
  stage_ids: string[]
  review_view?: 'active' | 'reviewed' | 'all'
  save?: boolean
}) {
  return api<CandidatesResponse>('/api/candidates/search', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function startAnalyze(body: AnalyzeOptions) {
  return api<JobState>('/api/analyze', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function fetchJob(jobId: string) {
  return api<JobState>(`/api/jobs/${jobId}`)
}

export function fetchReports(limit = 50) {
  return api<{ items: UiReportListItem[] }>(`/api/reports?limit=${limit}`)
}

export function fetchReport(reportId: number, includeMarkdown = false) {
  const q = includeMarkdown ? '?include_markdown=true' : ''
  return api<UiReportDetail>(`/api/reports/${reportId}${q}`)
}

export function fetchReportMarkdown(reportId: number) {
  return api<{ report_id: number; markdown: string }>(`/api/reports/${reportId}/markdown`)
}

export function saveDecision(reportId: number, decision: string, comment?: string) {
  return api<{ ok: boolean; decisions: Array<Record<string, unknown>>; candidate_review?: Record<string, unknown> | null }>(
    `/api/reports/${reportId}/rop-decision`,
    {
      method: 'POST',
      body: JSON.stringify({ decision, comment: comment || null }),
    },
  )
}

export function saveOutcome(reportId: number, outcome_type: string, notes?: string) {
  return api<{ ok: boolean; outcomes: Array<Record<string, unknown>> }>(
    `/api/reports/${reportId}/outcome`,
    {
      method: 'POST',
      body: JSON.stringify({ outcome_type, notes: notes || null }),
    },
  )
}

export function fetchCompactReview(entityType: 'lead' | 'deal', entityId: string, runId?: string) {
  const query = runId ? `?run_id=${encodeURIComponent(runId)}` : ''
  return api<CompactReview>(`/api/entity/${entityType}/${entityId}/compact-review${query}`)
}

export function startCompactRun(entityType: 'lead' | 'deal', entityId: string) {
  return api<CompactJob>(`/api/entity/${entityType}/${entityId}/compact-runs`, { method: 'POST' })
}

export function fetchCompactJob(jobId: string) {
  return api<CompactJob>(`/api/compact-jobs/${jobId}`)
}

export function fetchCompactEvidence(entityType: 'lead' | 'deal', entityId: string, evidenceId: string) {
  return api<Record<string, unknown>>(
    `/api/entity/${entityType}/${entityId}/compact-evidence/${encodeURIComponent(evidenceId)}`,
  )
}

export function saveCompactFeedback(
  entityType: 'lead' | 'deal',
  entityId: string,
  runId: string,
  result: 'correct' | 'partly_correct' | 'error',
  reason?: string,
  comment?: string,
) {
  return api<{ ok: boolean; feedback: Record<string, unknown> }>(
    `/api/entity/${entityType}/${entityId}/compact-runs/${runId}/feedback`,
    { method: 'PUT', body: JSON.stringify({ result, reason: reason || null, comment: comment || null }) },
  )
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function asString(value: unknown, fallback = ''): string {
  if (value === null || value === undefined) return fallback
  return String(value)
}

export function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item)).filter(Boolean)
}

/** LLM files may be saved as { analysis: {...} }; UI needs the inner object. */
export function unwrapAnalysis(value: unknown): Record<string, unknown> | null {
  const payload = asRecord(value)
  if (!Object.keys(payload).length) return null
  const inner = asRecord(payload.analysis)
  if (
    inner.rop_manager_message_block ||
    inner.main_risk ||
    inner.lead_state ||
    inner.deal_state ||
    inner.loss_diagnosis ||
    inner.money_path_diagnosis
  ) {
    return inner
  }
  if (
    payload.rop_manager_message_block ||
    payload.main_risk ||
    payload.lead_state ||
    payload.deal_state
  ) {
    return payload
  }
  return payload
}
