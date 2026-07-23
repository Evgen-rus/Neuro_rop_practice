export type Priority = 'high' | 'medium' | 'low'

export type LeadQualificationSummary = {
  category: 'A' | 'B' | 'C' | 'D' | 'E' | 'unknown'
  overall_status: string
  confirmed_count: number
  total_count: number
  statuses: Record<'budget' | 'authority' | 'need' | 'timeframe', string>
  decision_timing?: string | null
  need_or_launch_timing?: string | null
  route_status?: string | null
  controlled_return_status?: string | null
  controlled_return_date?: string | null
  recommended_return_date?: string | null
}

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
  lead_category?: string | null
  lead_analysis_available?: boolean
  lead_qualification?: LeadQualificationSummary | null
}

export type AnalysisPeriodPreset = 'today_and_previous_workday' | 'today' | 'previous_workday' | 'custom'

export type AnalysisProfileSettings = {
  timezone: string
  period_preset: AnalysisPeriodPreset
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
  completed_at?: string | null
  actual_cost?: Record<string, unknown> | null
  items?: DailySummaryItem[]
  job_states?: JobState[]
  results?: JobResult[]
}

export type EntityProgress = {
  key?: string
  entity_type: 'lead' | 'deal'
  entity_id: string
  stage: string
  status: string
  detail?: string
  current?: number | null
  total?: number | null
  attempt?: number | null
  max_attempts?: number | null
  error?: string | null
  started_at?: string | null
  updated_at?: string | null
}

export type DailySummaryItem = {
  id: number
  journey_key: string
  entity_type: 'lead' | 'deal'
  entity_id: string
  selected: number
  processing_status: string
  progress?: Partial<EntityProgress>
  report_id?: number | null
  error?: string | null
  candidate?: Candidate
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
  lead_categories: string[]
  bant_filter: '' | 'complete' | 'incomplete' | 'budget' | 'authority' | 'need' | 'timeframe' | 'negative' | 'unknown'
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
  lead_category?: string | null
  lead_route_status?: string | null
  lead_qualification?: LeadQualificationSummary | null
  bitrix_url?: string | null
  analysis?: Record<string, unknown> | null
  actual_cost?: {
    estimated_cost_usd?: number | null
    estimated_cost_rub?: number | null
    semantic_attempt_count?: number | null
  } | null
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
  entity_progress?: Record<string, EntityProgress>
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
  lead_category?: string | null
  lead_route_status?: string | null
  lead_qualification?: LeadQualificationSummary | null
  analysis_path?: string | null
  report_path?: string | null
  job_id?: string | null
  bitrix_url?: string | null
  share_token?: string | null
}

export type UiReportDetail = UiReportListItem & {
  report_json?: Record<string, unknown> | null
  report_markdown?: string | null
  decisions?: Array<Record<string, unknown>>
  outcomes?: Array<Record<string, unknown>>
  qualification_reviews?: Array<Record<string, unknown>>
  candidate_review?: Record<string, unknown> | null
  report_meta?: LeadReportMeta | null
  technical_log?: Record<string, unknown> | null
  model_context?: ModelContextSnapshot | null
  workflow?: LeadWorkflowState | null
  entity_history?: Array<Record<string, unknown>>
  markdown_available?: boolean
  technical_log_available?: boolean
}

export type LeadReportActivity = {
  event_id?: string | null
  type?: string | null
  channel?: string | null
  direction?: string | null
  direction_label?: string | null
  date?: string | null
  subject?: string | null
  text?: string | null
  completed?: boolean
  participant_name?: string | null
  source_label?: string | null
  contact_class?: string | null
  contact_label?: string | null
  classification_reason?: string | null
  duration_seconds?: number | null
  has_transcript?: boolean
  transcript_text?: string | null
}

export type ModelContextSnapshot = {
  history_text?: string | null
  transcript_text?: string | null
  transcript_used?: boolean
}

export type LeadReportMeta = {
  client_name?: string | null
  lead_title?: string | null
  lead_created_at?: string | null
  lead_modified_at?: string | null
  manager_id?: string | null
  stage_id?: string | null
  stage_name?: string | null
  last_contact?: LeadReportActivity | null
  last_attempt?: LeadReportActivity | null
  last_confirmed_contact?: LeadReportActivity | null
  last_internal_information?: LeadReportActivity | null
  current_task?: LeadReportActivity | null
  snapshot_generated_at?: string | null
}

export type LeadWorkflowState = {
  lead_id: string
  source_report_id?: number | null
  manager_review_text?: string | null
  manager_message_options?: string[]
  manager_full_review_text?: string | null
  manager_task_text?: string | null
  review_completed: boolean
  task_completed: boolean
  control_mode?: 'days' | 'date' | 'daily' | null
  control_days?: number | null
  control_date?: string | null
  control_completed: boolean
  status_label: string
  created_at?: string | null
  updated_at?: string | null
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

export function previewAnalysisProfile(
  profileId: number,
  period: { period_preset: AnalysisPeriodPreset; date_from?: string; date_to?: string },
) {
  return api<DailyPreview>(`/api/analysis-profiles/${profileId}/preview`, {
    method: 'POST',
    body: JSON.stringify(period),
  })
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
  return api<{ summary: DailySummaryRun; jobs: JobState[]; started_count: number; reused_count: number }>(`/api/daily-summaries/${runId}/start`, {
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
  lead_categories?: string[]
  bant_filter?: string
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
  for (const value of params.lead_categories || []) query.append('lead_categories', value)
  if (params.bant_filter) query.set('bant_filter', params.bant_filter)
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
  lead_categories?: string[]
  bant_filter?: string
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

export function fetchReviewReport(shareToken: string) {
  return api<UiReportDetail>(`/api/review/${encodeURIComponent(shareToken)}`)
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

export function saveLeadWorkflow(leadId: string, payload: Partial<LeadWorkflowState>) {
  return api<LeadWorkflowState>(`/api/leads/${encodeURIComponent(leadId)}/workflow`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
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

export function saveQualificationReview(
  reportId: number,
  body: {
    is_correct: boolean
    issue_fields?: string[]
    corrected_statuses?: Record<string, string>
    corrected_category?: string | null
    comment?: string | null
  },
) {
  return api<{ ok: boolean; qualification_reviews: Array<Record<string, unknown>> }>(
    `/api/reports/${reportId}/qualification-review`,
    { method: 'POST', body: JSON.stringify(body) },
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
