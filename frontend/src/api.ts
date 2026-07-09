export type Priority = 'high' | 'medium' | 'low'

export type Candidate = {
  entity_type: 'lead' | 'deal'
  entity_id: string
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
}

export type CandidatesResponse = {
  days: number
  limit: number
  entity_type: string
  generated_at: string
  summary: {
    total_scored: number
    returned: number
    high: number
    medium: number
    low: number
    already_analyzed: number
  }
  candidates: Candidate[]
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
}

export type UiReportDetail = UiReportListItem & {
  report_json?: Record<string, unknown> | null
  report_markdown?: string | null
  decisions?: Array<Record<string, unknown>>
  outcomes?: Array<Record<string, unknown>>
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

export function fetchCandidates(params: {
  entity_type?: string
  days?: number
  limit?: number
  priority?: string
}) {
  const query = new URLSearchParams()
  if (params.entity_type) query.set('entity_type', params.entity_type)
  if (params.days !== undefined) query.set('days', String(params.days))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  if (params.priority) query.set('priority', params.priority)
  return api<CandidatesResponse>(`/api/candidates?${query.toString()}`)
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
  return api<{ ok: boolean; decisions: Array<Record<string, unknown>> }>(
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
