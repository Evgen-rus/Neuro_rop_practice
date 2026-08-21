export type Priority = 'high' | 'medium' | 'low'

export type AuthRole = 'admin' | 'rop' | 'manager'

export type AuthUser = {
  id: number
  login: string
  role: AuthRole
  manager_id: string | null
  is_active: true
}

export type AuthMeResponse = {
  authenticated: true
  user: AuthUser
}

export class ApiError extends Error {
  readonly status: number
  readonly retryAfter: string | null

  constructor(message: string, status: number, retryAfter: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

type AuthEventHandler = () => void
let unauthorizedHandler: AuthEventHandler | null = null

export function setUnauthorizedHandler(handler: AuthEventHandler | null) {
  unauthorizedHandler = handler
}

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

export type DealControlTask = {
  id: number
  deal_id: string
  task_text: string
  touch_type?: string | null
  expected_result?: string | null
  source_kind?: 'manual' | 'neuro_rop' | null
  source_report_id?: number | null
  recommendation_state?: DealControlRecommendationState | null
  attention_priority?: number | null
  needs_follow_up?: boolean | null
  recommendation_reason?: string | null
  due_at: string
  local_status: 'active' | 'completed' | 'cancelled'
  crm_execution_status: 'not_reflected' | 'crm_open' | 'crm_closed' | 'match_review'
  crm_match_activity_id?: string | null
  crm_match_confidence?: string | null
  crm_match_candidate_completed?: number | null
  crm_match_confirmed?: number
  business_result_status: 'no_result' | 'client_fact' | 'next_step' | 'needs_rop_review'
  business_result_note?: string | null
  view_status?: string
  time_bucket?: 'overdue' | 'today' | 'tomorrow' | 'future' | 'completed_today' | 'completed' | 'cancelled'
  guidance_revision?: number
  guidance?: DealTaskGuidance | null
  baseline?: {
    task_id: number
    source_report_id?: number | null
    created_at: string
    deal_snapshot: Record<string, unknown>
  } | null
  latest_outcome?: DealControlTaskOutcome | null
  crm_facts?: DealControlCrmFact[]
}

export type DealControlRecommendationState = 'not_done' | 'attempted' | 'contacted' | 'achieved' | 'unconfirmed'

export function isNeuroRopTask(task?: Pick<DealControlTask, 'source_kind'> | null) {
  return task?.source_kind === 'neuro_rop'
}

export type DealControlTaskOutcome = {
  id: number
  task_id: number
  contact_status: 'not_attempted' | 'attempt_no_contact' | 'confirmed_contact' | 'unknown'
  result_status: 'pending' | 'achieved' | 'partial' | 'postponed' | 'refused' | 'not_applicable' | 'needs_rop_review'
  result_note?: string | null
  next_step_text?: string | null
  next_step_at?: string | null
  evidence_kind?: 'crm_activity' | 'transcript' | 'manager_confirmation' | 'rop_confirmation' | null
  evidence_id?: string | null
  source_role: 'manager' | 'rop'
  created_at: string
}

export type DealControlCrmFact = {
  id: number
  task_id: number
  activity_id?: string | null
  fact_kind: string
  summary?: string | null
  occurred_at?: string | null
  contact_class: 'attempt' | 'confirmed_contact' | 'internal_information' | 'unknown' | 'deal_progress'
  review_status: 'candidate' | 'confirmed' | 'rejected'
  fact_key?: string | null
}

export type DealControlMetricValues = {
  tasks: number
  actions_completed: number
  confirmed_contacts: number
  target_results: number
  next_steps: number
  stage_progressed: number
  deals_won: number
}

export type DealControlMetrics = {
  overall: DealControlMetricValues
  with_guidance: DealControlMetricValues
  without_guidance: DealControlMetricValues
  cancelled_tasks: number
  note: string
}

export type DealTaskGuidanceContent = {
  task_focus: string
  expected_outcome: string
  known_facts: string[]
  missing_facts: string[]
  contact_goal: string
  contact_questions: string[]
  ready_text: string
  crm_checklist: string[]
}

export type DealTaskGuidance = {
  id: number
  task_id: number
  task_revision: number
  source_report_id: number
  content: DealTaskGuidanceContent
  created_at: string
  is_stale: boolean
}

export type DealTaskGuidanceJob = {
  job_id: string
  task_id: number
  deal_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  stage: 'queued' | 'context' | 'llm' | 'saving' | 'done' | 'error'
  detail: string
  percent: number
  guidance_id?: number | null
  error?: string | null
}

export type DealControlBitrixTask = {
  activity_id: string
  task_id?: string
  responsible_id?: string
  subject: string
  description?: string
  deadline?: string | null
  time_bucket: 'overdue' | 'today' | 'tomorrow' | 'future' | 'unscheduled'
  completed: boolean
  bitrix_completed_at?: string | null
  local_completed: boolean
  local_completed_at?: string | null
  local_completed_by?: 'manager' | 'rop' | null
  completion_state: 'open' | 'local' | 'bitrix'
  provider_id: string
}

export type DealControlCommunicationItem = {
  event_id: string
  channel: 'call' | 'email' | 'message' | string
  direction: 'incoming' | 'outgoing' | 'unknown' | string
  occurred_at: string
  subject?: string
  duration_seconds?: number | null
  contact_class?: string
}

export type DealCallTranscript = {
  deal_id: string
  event_id: string
  text: string
  truncated: boolean
}

export type DealCommunicationContent = {
  deal_id: string
  event_id: string
  channel: string
  text: string
  is_excerpt: boolean
  truncated: boolean
}

export type DealControlCommunicationsToday = {
  date: string
  available: boolean
  target: number
  completed: number
  progress_percent: number
  calls: number
  messages: number
  duration_seconds: number
  items: DealControlCommunicationItem[]
}

export type DealControlChecklistItem = {
  id: string
  text: string
  completed: boolean
  completed_at?: string | null
  completed_by?: 'manager' | null
  source: 'missing' | 'focus' | 'crm' | string
  change_kind?: 'new' | 'carried' | 'reopened' | 'completed' | 'returned' | string
}

export type DealControlChecklist = {
  business_date?: string | null
  revision?: number
  source_report_id?: number | null
  items: DealControlChecklistItem[]
  completed: number
  total: number
  progress_percent: number
}

export type CommunicationQualityAuditCriterion = {
  score: 0 | 1 | null
}

export type CommunicationQualityAudit = {
  status: 'assessed' | 'insufficient_evidence'
  scope_summary: string
  criteria: {
    next_action: CommunicationQualityAuditCriterion
    value_development: CommunicationQualityAuditCriterion
    data_collection: CommunicationQualityAuditCriterion
  }
  zero_reasons: Array<{
    criterion: 'next_action' | 'value_development' | 'data_collection'
    explanation: string
    quote: string
  }>
  summary_for_rop?: string | null
  insufficient_reason?: string | null
}

export type DailyControlStatus = 'red' | 'yellow' | 'green'

export type DailyControlQualityCriterion = {
  score: 0 | 1 | null
  verdict: string
}

export type DailyControlDeal = {
  deal_id: string
  title?: string | null
  manager_id?: string | null
  manager_name?: string | null
  stage_id?: string | null
  stage_name?: string | null
  amount?: string | null
  currency_id?: string | null
  status: DailyControlStatus
  status_label: string
  attention_reason: string
  quality: {
    status: 'assessed' | 'insufficient_evidence' | 'missing'
    criteria: {
      next_action: DailyControlQualityCriterion
      value_development: DailyControlQualityCriterion
      data_collection: DailyControlQualityCriterion
    }
    confirmed_count: number | null
    total: number
    scope_summary?: string | null
    zero_reasons: Array<{ criterion?: string | null; explanation?: string | null; quote?: string | null }>
    summary_for_rop?: string | null
    insufficient_reason?: string | null
  }
  summary_for_rop?: string | null
  direct_question: string
  generic_question: string
  ai_context: {
    current_situation: string
    rop_focus: string
    what_to_check_now: string
    manager_coaching: string
    known: string[]
    unknowns: string[]
    strengths: string[]
    weaknesses: string[]
  }
  script: string
  script_variants: string[]
  communications_today: DealControlCommunicationsToday & {
    unavailable?: boolean
    content_available?: boolean
    items: Array<DealControlCommunicationItem & { content_available?: boolean }>
  }
  checklist: {
    business_date?: string | null
    revision?: number
    source_report_id?: number | null
    completed: number
    total: number
    progress_percent?: number
    items: Array<DealControlChecklistItem & { why?: string | null }>
  }
  has_analysis: boolean
  analysis_created_at?: string | null
}

export type DealControlDeal = {
  deal_id: string
  source: 'initial' | 'pipeline'
  title?: string | null
  manager_id?: string | null
  manager_name?: string | null
  ownership: 'own' | 'foreign' | 'unassigned'
  is_own: boolean
  read_only: boolean
  can_open: boolean
  can_edit: boolean
  can_run_analysis: boolean
  can_run_paid_ai: boolean
  stage_id?: string | null
  stage_name?: string | null
  pipeline_id?: string | null
  amount?: string | null
  currency_id?: string | null
  created_at_crm?: string | null
  modified_at_crm?: string | null
  probability?: number | null
  expected_payment_period?: string | null
  next_control_at?: string | null
  bitrix_tasks: DealControlBitrixTask[]
  communications_today: DealControlCommunicationsToday
  primary_bitrix_task?: DealControlBitrixTask | null
  tasks: DealControlTask[]
  current_task?: DealControlTask | null
  manager_situation?: ManagerSituationState | null
  checklist: DealControlChecklist
  coaching: {
    report_id?: number | null
    analysis_created_at?: string | null
    current_situation?: string
    strengths: string[]
    weaknesses: string[]
    rop_focus?: string
    what_to_check_now?: string
    manager_coaching?: string
    known: string[]
    unknowns: string[]
    contact_goal?: string
    questions: string[]
    script?: string
    script_variants: string[]
    crm_checklist: string[]
    script_channel?: string
    rop_task_hint?: string
    expected_crm_update?: string
    communication_quality_audit?: CommunicationQualityAudit | null
    manager_situation?: ManagerSituationState | null
    direct_manager_question?: string
  }
  review?: DailyControlDeal
}

export type DealControlDashboard = {
  scope: { initial_deal_ids: string[]; manager_ids: string[]; pipeline_id: string; pipeline_ids?: string[]; configured: boolean; updated_at?: string }
  generated_at: string
  sync_message?: string | null
  sync_errors: string[]
  summary: {
    active_deals: number
    portfolio_amount: number
    tasks_total: number
    tasks_today: number
    tasks_tomorrow: number
    tasks_future: number
    tasks_overdue: number
    tasks_completed_today: number
    tasks_missing: number
    tasks_plan_today: number
    average_probability?: number | null
  }
  outcome_metrics: DealControlMetrics
  deals: DealControlDeal[]
}

export type DailyControlFreshnessState = 'current' | 'stale' | 'historical' | 'missing'
export type DailyControlCreationKind = 'manual' | 'automatic_planning'

export type DailyControlManager = {
  manager_id?: string | null
  manager_name: string
  deals_count: number
  checklist_completed: number
  checklist_total: number
  calls: number
  messages: number
  talk_seconds: number
  red: number
  yellow: number
  green: number
}

export type DailyControlSnapshot = {
  team: {
    traffic_light: { red: number; yellow: number; green: number }
    deals_total: number
    no_movement: { count: number; total: number }
    calls: number
    messages: number
    talk_seconds: number
  }
  managers: DailyControlManager[]
  deals: DailyControlDeal[]
  source_warnings: string[]
  communications_unavailable_count?: number
}

export type DailyControlFreshness = {
  state: DailyControlFreshnessState
  label: string
  is_latest: boolean
  live_watermark: string
  report_watermark?: string | null
}

export type DailyControlGeneration = {
  status: 'queued' | 'running' | 'done' | 'error'
  started_at?: string | null
  finished_at?: string | null
  report_id?: number | null
  error?: string | null
}

export type DailyControlReportMeta = {
  id: number
  business_date: string
  creation_kind: DailyControlCreationKind
  started_at: string
  cutoff_at: string
  created_at: string
  source_watermark: string
  automatic_analysis_run_id?: number | null
  source_status?: string | null
  warnings: string[]
  error?: string | null
  position?: number
  total?: number
  freshness?: DailyControlFreshness
}

export type DailyControlHistory = {
  reports: DailyControlReportMeta[]
  latest_id: number | null
  total: number
  live_watermark: string
  generation: DailyControlGeneration | null
}

export type DailyControlReport = DailyControlReportMeta & {
  snapshot: DailyControlSnapshot
  freshness: DailyControlFreshness
  previous_id?: number | null
  next_id?: number | null
  generation?: DailyControlGeneration | null
}

export type ManagerSituationState = {
  state: 'pending' | 'confirmed' | 'refined'
  review_id?: number | null
  source_report_id?: number | null
  revision?: number | null
  manager_context?: string | null
  confirmed_at?: string | null
  business_date?: string | null
  last_confirmation_business_date?: string | null
  is_current: boolean
}

export type ManagerSituationJob = {
  job_id: string
  deal_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  stage: 'queued' | 'context' | 'llm' | 'saving' | 'done' | 'error'
  detail: string
  percent: number
  situation_id?: number | null
  situation?: ManagerSituationState | null
  error?: string | null
}

type ManagerQuickHelpCommonContent = {
  situation_summary: string
  next_action: string
  expected_result: string
  crm_checklist: string[]
}

export type ManagerQuickHelpLegacyContent = ManagerQuickHelpCommonContent & {
  answer_contract: 'legacy'
  client_messages: Record<'calm' | 'confident' | 'direct', string>
  recommended_client_tone: 'calm' | 'confident' | 'direct'
  call_scripts: Record<'soft' | 'business' | 'direct', string>
  recommended_call_tone: 'soft' | 'business' | 'direct'
}

export type ManagerQuickHelpStrategy = 'primary' | 'alternative' | 'pattern_break'

export type ManagerQuickHelpStrategyContent = ManagerQuickHelpCommonContent & {
  answer_contract: 'strategy_v1'
  client_messages: Record<ManagerQuickHelpStrategy, string>
  call_scripts: Record<ManagerQuickHelpStrategy, string>
  recommended_strategy: ManagerQuickHelpStrategy
  recommended_channel: 'message' | 'call'
  fallback_action: string
}

export type ManagerLifehack = {
  tactic_id: string
  title: string
  action: string
  why_relevant: string
  conditions: string
}

export type ManagerPressureLever = {
  title: string
  rationale: string
}

export type ManagerQuickHelpStrategyV2Content = ManagerQuickHelpCommonContent & {
  answer_contract: 'strategy_v2'
  client_messages: Record<ManagerQuickHelpStrategy, string>
  lifehacks: ManagerLifehack[]
  fallback_action: string
}

export type ManagerAssistantMode = 'push' | 'reanimator'

export type ManagerQuickHelpStrategyV3Content = ManagerQuickHelpCommonContent & {
  answer_contract: 'strategy_v3'
  mode: ManagerAssistantMode
  pressure_lever: ManagerPressureLever
  strategy_labels: Record<ManagerQuickHelpStrategy, string>
  client_messages: Record<ManagerQuickHelpStrategy, string>
  lifehacks: ManagerLifehack[]
  fallback_action: string
}

export type ManagerQuickHelpContent = ManagerQuickHelpLegacyContent | ManagerQuickHelpStrategyContent | ManagerQuickHelpStrategyV2Content | ManagerQuickHelpStrategyV3Content

export type ManagerQuickHelpEntry = {
  id: number
  deal_id: string
  source_report_id?: number | null
  situation_review_id?: number | null
  mode?: ManagerAssistantMode | null
  origin?: 'auto' | 'manager' | null
  turn_id?: string | null
  question: string
  content: ManagerQuickHelpContent
  created_at: string
  model_meta?: Record<string, unknown> | null
}

export type ManagerQuickHelpJob = {
  job_id: string
  deal_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  stage: 'queued' | 'context' | 'llm' | 'saving' | 'done' | 'error'
  detail: string
  percent: number
  mode?: ManagerAssistantMode | null
  origin?: 'auto' | 'manager' | null
  quick_help_id?: number | null
  saved_by_mode?: Partial<Record<ManagerAssistantMode, number>>
  reused?: boolean
  entry_id?: number | null
  entry?: ManagerQuickHelpEntry | null
  error?: string | null
}

export type ManagerQuickHelpHistory = {
  entries: ManagerQuickHelpEntry[]
  has_more?: boolean
  next_before_id?: number | null
}

export type ManagerMessageScriptBlock = {
  block_id: string
  title: string
  objective: string
  suggested_phrases: string[]
  listen_for: string[]
  transition: string
  relevant_objection_ids: string[]
}

export type ManagerCallScriptBlock = {
  block_id: string
  title: string
  objective: string
  spoken_text: string
  clarifying_question: string
  listen_for: string[]
  transition: string
  relevant_objection_ids: string[]
}

export type ManagerFullScriptBlock = ManagerMessageScriptBlock | ManagerCallScriptBlock

export type ManagerMessageScriptContent = {
  script_contract: 'conversation_script_v1'
  selected_strategy: ManagerQuickHelpStrategy
  conversation_goal: string
  blocks: ManagerMessageScriptBlock[]
  closing_agreement: string
  relevant_tactic_ids: string[]
}

export type ManagerCallScriptContent = {
  script_contract: 'conversation_script_v2'
  selected_strategy: ManagerQuickHelpStrategy
  conversation_goal: string
  blocks: ManagerCallScriptBlock[]
  closing_agreement: string
  relevant_tactic_ids: string[]
}

export type ManagerConversationScriptContent = ManagerMessageScriptContent | ManagerCallScriptContent

export type ManagerEmailContent = {
  email_contract: 'manager_email_v1'
  selected_strategy: ManagerQuickHelpStrategy
  subject: string
  greeting: string
  context: string
  questions: string[]
  value_point: string
  call_to_action: string
  closing: string
}

export type ManagerFullScriptContent = ManagerConversationScriptContent | ManagerEmailContent
export type ManagerFullScriptMode = 'message' | 'call' | 'email'

export function isCallScriptContent(
  script: ManagerConversationScriptContent,
): script is ManagerCallScriptContent {
  return script.script_contract === 'conversation_script_v2'
}

export type ManagerFullScriptRecord = {
  id: number
  quick_help_id: number
  selected_strategy: ManagerQuickHelpStrategy
  content: ManagerFullScriptContent
  created_at: string
}

export type ManagerFullScriptJob = {
  job_id: string
  deal_id: string
  quick_help_id: number
  selected_strategy: ManagerQuickHelpStrategy
  script_mode: ManagerFullScriptMode
  status: 'queued' | 'running' | 'done' | 'error'
  stage: 'queued' | 'context' | 'llm' | 'saving' | 'done' | 'error'
  detail: string
  percent: number
  script_id?: number | null
  reused?: boolean
  error?: string | null
}

export type ManagerObjectionHandling = {
  items: Array<{
    objection_id: string
    objection: string
    manager_reply: string
    follow_up_question: string
    next_step_goal: string
    what_not_to_do: string
  }>
}

export type ManagerDiscProfile = {
  primary_style: 'D' | 'I' | 'S' | 'C'
  secondary_style?: 'D' | 'I' | 'S' | 'C' | null
  profile_confidence: 'low' | 'medium' | 'high'
}

export type ManagerFullScriptWorkspace = {
  script: ManagerFullScriptRecord | null
  script_mode: ManagerFullScriptMode
  disc_profile: ManagerDiscProfile | null
  checklist: Record<string, unknown>
  objection_handling: ManagerObjectionHandling | null
}

export type ManagerAssistantTimelineEntry = {
  id: string
  kind: 'assistant_request' | 'communication' | 'communication_completed' | string
  occurred_at?: string | null
  text: string
  channel?: string | null
  contact_class?: string | null
}

export type DealContextCriticalFact = {
  fact_id: string
  category: string
  fact: string
  status: 'confirmed' | 'needs_confirmation' | 'conflicted' | 'outdated' | string
  importance: 'high' | 'medium' | 'low' | string
  observed_at?: string | null
  source_type: string
  evidence: string[]
}

export type DealContextTurningPoint = {
  turning_point_id: string
  occurred_at?: string | null
  title: string
  what_happened: string
  impact: string
  status: string
  evidence: string[]
}

export type DealContextPainPoint = {
  pain_id: string
  title: string
  description: string
  status: string
  impact: string
  evidence: string[]
}

export type DealContextPressureLever = {
  lever_id: string
  type: string
  title: string
  fact: string
  why_important: string
  business_consequence: string
  basis_status: 'confirmed' | 'inferred' | 'needs_confirmation' | string
  status: string
  ai_priority?: 1 | 2 | 3 | null
  manual_priority?: 1 | 2 | 3 | null
  evidence: string[]
}

export type DealContextDealCard = {
  title?: string
  company?: string
  equipment?: string
  manufacturing_days?: string | number | null
  amount?: string | number | null
  currency_id?: string | null
  responsible?: string
  stage?: string
}

export type DealContextDecisionPath = {
  decision_maker: string
  influencers: string[]
  approval_path: string
  current_step_owner: string
  basis_status: string
  evidence: string[]
}

export type DealContextCommitment = {
  commitment_id: string
  party: string
  promise: string
  due_at?: string | null
  status: string
  basis_status: string
  evidence: string[]
}

export type DealContextJourneyEntry = {
  entry_id: string
  occurred_at?: string | null
  title: string
  what_happened: string
  learned: string[]
  missing: string[]
  status: string
}

export type DealContextBantItem = {
  status?: string
  evidence?: string[]
  missing_facts?: string[]
  decision_timing?: string | null
  decision_timing_status?: string
  need_or_launch_timing?: string | null
  need_or_launch_timing_status?: string
}

export type DealContextSnapshot = {
  deal_card?: DealContextDealCard | null
  current_truth: {
    client_profile: string
    current_need: string
    desired_outcome: string
    current_status: string
    current_task: string
    next_checkpoint?: string | null
    next_step_owner: string
  }
  decision_path?: DealContextDecisionPath | null
  commitments?: DealContextCommitment[]
  critical_facts: DealContextCriticalFact[]
  turning_points: DealContextTurningPoint[]
  journey?: DealContextJourneyEntry[]
  pain_points: DealContextPainPoint[]
  pressure_levers: DealContextPressureLever[]
  open_questions: string[]
  source_conflicts: Array<{ description: string; sources: string[]; next_check: string }>
  bant?: {
    budget?: DealContextBantItem
    authority?: DealContextBantItem
    need?: DealContextBantItem
    timeframe?: DealContextBantItem
    overall_status?: string
    missing_facts?: string[]
    next_question?: string | null
  } | null
  solution_fit?: { equipment_type?: string; status?: string; missing_facts?: string[] } | null
  commercial_fit?: { confirmed_budget_rub?: number | null; new_equipment_budget_status?: string } | null
  money_path?: {
    stuck_point?: string
    why_money_is_at_risk?: string
    current_owner_of_next_step?: string
    next_required_fact?: string
    evidence?: string[]
  } | null
  payment_blocker?: {
    applicable?: boolean
    blocker_type?: string
    payer?: string
    current_status?: string
    missing_confirmation?: string[]
    next_actions?: string[]
  } | null
  competitor?: {
    applicable?: boolean
    competitor_type?: string
    defense_points?: string[]
    questions_to_client?: string[]
    risk_if_not_defended?: string
  } | null
}

export type ManagerAssistantWorkspace = {
  started: boolean
  entries: ManagerQuickHelpEntry[]
  current_by_mode?: Partial<Record<ManagerAssistantMode, ManagerQuickHelpEntry | null>>
  source_report_id?: number | null
  situation_review_id?: number | null
  timeline: ManagerAssistantTimelineEntry[]
  disc_profile?: ManagerDiscProfile | null
  context: {
    stage: string
    current_task: string
    last_communication?: {
      event_id?: string | null
      channel?: string | null
      occurred_at?: string | null
      text: string
    } | null
    main_risk: string
    deal_context?: DealContextSnapshot | null
    report?: { report_id?: number | null; markdown_available: boolean } | null
  }
}

type ManagerSituationResponse = {
  manager_situation?: ManagerSituationState
  situation?: ManagerSituationState
}

type ApiOptions = {
  suppressUnauthorizedEvent?: boolean
}

async function api<T>(path: string, init?: RequestInit, options: ApiOptions = {}): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!(init?.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const payload = await response.json()
      detail = typeof payload.detail === 'string' ? payload.detail : response.statusText
    } catch {
      // ignore
    }
    if (response.status === 401 && !options.suppressUnauthorizedEvent) unauthorizedHandler?.()
    const message = response.status === 403
      ? 'Недостаточно прав для этого действия'
      : detail || 'Request failed'
    throw new ApiError(message, response.status, response.headers.get('Retry-After'))
  }
  if (response.status === 204) return undefined as T
  const body = await response.text()
  return (body ? JSON.parse(body) : undefined) as T
}

export function fetchCurrentUser() {
  return api<AuthMeResponse>('/api/auth/me')
}

export function login(loginValue: string, password: string) {
  return api<AuthMeResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ login: loginValue, password }),
  }, { suppressUnauthorizedEvent: true })
}

export function logout() {
  return api<void>('/api/auth/logout', { method: 'POST' }, { suppressUnauthorizedEvent: true })
}

function normalizeDealControlDashboard(payload: DealControlDashboard): DealControlDashboard {
  const emptyCommunications: DealControlCommunicationsToday = {
    date: '',
    available: false,
    target: 0,
    completed: 0,
    progress_percent: 0,
    calls: 0,
    messages: 0,
    duration_seconds: 0,
    items: [],
  }
  const emptyChecklist: DealControlChecklist = {
    items: [],
    completed: 0,
    total: 0,
    progress_percent: 0,
  }
  const emptyCoaching: DealControlDeal['coaching'] = {
    strengths: [],
    weaknesses: [],
    known: [],
    unknowns: [],
    questions: [],
    script_variants: [],
    crm_checklist: [],
  }
  const deals = (Array.isArray(payload.deals) ? payload.deals : []).map((deal) => {
    const foreignProjection = deal.read_only === true && deal.can_open !== true
    return {
      ...deal,
      ownership: deal.ownership === 'own' || deal.ownership === 'foreign' || deal.ownership === 'unassigned'
        ? deal.ownership
        : 'unassigned',
      is_own: deal.is_own === true,
      read_only: deal.read_only === true,
      can_open: deal.can_open === true,
      can_edit: deal.can_edit === true,
      can_run_analysis: deal.can_run_analysis === true,
      can_run_paid_ai: deal.can_run_paid_ai === true,
      bitrix_tasks: foreignProjection ? [] : (Array.isArray(deal.bitrix_tasks) ? deal.bitrix_tasks : []),
      communications_today: foreignProjection ? emptyCommunications : (deal.communications_today || emptyCommunications),
      tasks: foreignProjection ? [] : (Array.isArray(deal.tasks) ? deal.tasks : []),
      current_task: foreignProjection ? null : (deal.current_task || null),
      manager_situation: foreignProjection ? null : (deal.manager_situation || null),
      checklist: foreignProjection ? emptyChecklist : (deal.checklist || emptyChecklist),
      coaching: foreignProjection ? emptyCoaching : (deal.coaching || emptyCoaching),
      review: foreignProjection ? undefined : deal.review,
    }
  })
  return { ...payload, deals }
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

export function fetchDealControl() {
  return api<DealControlDashboard>('/api/deal-control', { cache: 'no-store' }).then(normalizeDealControlDashboard)
}

export function fetchDealCallTranscript(dealId: string, eventId: string) {
  return api<DealCallTranscript>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/communications/${encodeURIComponent(eventId)}/transcript`,
  )
}

export function fetchDealCommunicationContent(dealId: string, eventId: string) {
  return api<DealCommunicationContent>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/communications/${encodeURIComponent(eventId)}/content`,
  )
}

export function confirmManagerSituation(dealId: string) {
  return api<ManagerSituationResponse>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/situation/confirm`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function startManagerSituationRefinement(dealId: string, context: string, confirmPaid = true) {
  return api<ManagerSituationJob>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/situation/refine`, {
    method: 'POST',
    body: JSON.stringify({ context, confirm_paid: confirmPaid }),
  })
}

export function fetchManagerSituationJob(jobId: string) {
  return api<ManagerSituationJob>(`/api/deal-control/situation-jobs/${encodeURIComponent(jobId)}`)
}

export function startManagerQuickHelp(
  dealId: string,
  question = '',
  confirmPaid = true,
  mode?: ManagerAssistantMode,
) {
  return api<ManagerQuickHelpJob>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/quick-help`, {
    method: 'POST',
    body: JSON.stringify({ question, confirm_paid: confirmPaid, mode: mode ?? null }),
  })
}

export function fetchManagerQuickHelpJob(jobId: string) {
  return api<ManagerQuickHelpJob>(`/api/deal-control/quick-help-jobs/${encodeURIComponent(jobId)}`)
}

export async function fetchManagerQuickHelpHistory(dealId: string, limit = 20, beforeId?: number) {
  const query = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, limit))) })
  if (beforeId != null) query.set('before_id', String(beforeId))
  const payload = await api<ManagerQuickHelpHistory & { items?: unknown[] } | unknown[]>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/quick-help-history?${query.toString()}`,
  )
  const rawEntries = Array.isArray(payload)
    ? payload
    : Array.isArray(payload.items)
      ? payload.items
      : Array.isArray(payload.entries)
        ? payload.entries
        : []
  const entries = rawEntries
    .map(normalizeManagerQuickHelpEntry)
    .filter((item): item is ManagerQuickHelpEntry => Boolean(item))
  return {
    entries,
    has_more: !Array.isArray(payload) ? payload.has_more : undefined,
    next_before_id: !Array.isArray(payload) ? payload.next_before_id : undefined,
  }
}

function normalizeManagerQuickHelpEntry(value: unknown): ManagerQuickHelpEntry | null {
  const record = asRecord(value)
  const nonEmptyRecord = (item: unknown) => {
    const candidate = asRecord(item)
    return Object.keys(candidate).length ? candidate : null
  }
  let content = nonEmptyRecord(record.content)
  if (!content) content = nonEmptyRecord(record.answer)
  if (!content && typeof record.answer_json === 'string') {
    try { content = nonEmptyRecord(JSON.parse(record.answer_json)) } catch { content = null }
  }
  if (!content) return null
  const id = Number(record.id ?? record.quick_help_id)
  if (!Number.isFinite(id)) return null
  const legacyClientMessage = asString(content.client_message)
  const legacyCallScript = asString(content.call_script)
  const clientMessages = asRecord(content.client_messages)
  const callScripts = asRecord(content.call_scripts)
  const recommendedClientTone = asString(content.recommended_client_tone)
  const recommendedCallTone = asString(content.recommended_call_tone)
  const answerContract = asString(content.answer_contract)
  const recommendedStrategy = asString(content.recommended_strategy)
  const recommendedChannel = asString(content.recommended_channel)
  const lifehacks = (Array.isArray(content.lifehacks) ? content.lifehacks : []).map((item) => {
    const value = asRecord(item)
    return {
      tactic_id: asString(value.tactic_id),
      title: asString(value.title),
      action: asString(value.action),
      why_relevant: asString(value.why_relevant),
      conditions: asString(value.conditions),
    }
  }).filter((item) => item.tactic_id && item.title && item.action)
  const commonContent = {
    situation_summary: asString(content.situation_summary) || asString(content.problem_summary),
    next_action: asString(content.next_action) || asString(content.recommended_action),
    expected_result: asString(content.expected_result),
    crm_checklist: asStringList(content.crm_checklist),
  }
  const strategyLabels = asRecord(content.strategy_labels)
  const pressureLever = asRecord(content.pressure_lever)
  const contentMode = asString(content.mode)
  const normalizedContent: ManagerQuickHelpContent = answerContract === 'strategy_v3'
    ? {
        ...commonContent,
        answer_contract: 'strategy_v3',
        mode: contentMode === 'push' ? 'push' : 'reanimator',
        pressure_lever: {
          title: asString(pressureLever.title),
          rationale: asString(pressureLever.rationale),
        },
        strategy_labels: {
          primary: asString(strategyLabels.primary),
          alternative: asString(strategyLabels.alternative),
          pattern_break: asString(strategyLabels.pattern_break),
        },
        client_messages: {
          primary: asString(clientMessages.primary),
          alternative: asString(clientMessages.alternative),
          pattern_break: asString(clientMessages.pattern_break),
        },
        lifehacks,
        fallback_action: asString(content.fallback_action),
      }
    : answerContract === 'strategy_v2'
    ? {
        ...commonContent,
        answer_contract: 'strategy_v2',
        client_messages: {
          primary: asString(clientMessages.primary),
          alternative: asString(clientMessages.alternative),
          pattern_break: asString(clientMessages.pattern_break),
        },
        lifehacks,
        fallback_action: asString(content.fallback_action),
      }
    : answerContract === 'strategy_v1'
    ? {
        ...commonContent,
        answer_contract: 'strategy_v1',
        client_messages: {
          primary: asString(clientMessages.primary),
          alternative: asString(clientMessages.alternative),
          pattern_break: asString(clientMessages.pattern_break),
        },
        call_scripts: {
          primary: asString(callScripts.primary),
          alternative: asString(callScripts.alternative),
          pattern_break: asString(callScripts.pattern_break),
        },
        recommended_strategy: ['primary', 'alternative', 'pattern_break'].includes(recommendedStrategy)
          ? recommendedStrategy as ManagerQuickHelpStrategy
          : 'primary',
        recommended_channel: recommendedChannel === 'call' ? 'call' : 'message',
        fallback_action: asString(content.fallback_action),
      }
    : {
        ...commonContent,
        answer_contract: 'legacy',
        client_messages: {
          calm: asString(clientMessages.calm) || legacyClientMessage,
          confident: asString(clientMessages.confident) || legacyClientMessage,
          direct: asString(clientMessages.direct) || legacyClientMessage,
        },
        recommended_client_tone: ['calm', 'confident', 'direct'].includes(recommendedClientTone)
          ? recommendedClientTone as ManagerQuickHelpLegacyContent['recommended_client_tone']
          : 'calm',
        call_scripts: {
          soft: asString(callScripts.soft) || legacyCallScript,
          business: asString(callScripts.business) || legacyCallScript,
          direct: asString(callScripts.direct) || legacyCallScript,
        },
        recommended_call_tone: ['soft', 'business', 'direct'].includes(recommendedCallTone)
          ? recommendedCallTone as ManagerQuickHelpLegacyContent['recommended_call_tone']
          : 'business',
      }
  const entryMode = asString(record.mode) || (answerContract === 'strategy_v3' ? contentMode : '')
  const entryOrigin = asString(record.origin)
  return {
    id,
    deal_id: asString(record.deal_id),
    source_report_id: record.source_report_id == null ? null : Number(record.source_report_id),
    situation_review_id: record.situation_review_id == null ? null : Number(record.situation_review_id),
    mode: entryMode === 'push' ? 'push' : entryMode === 'reanimator' ? 'reanimator' : null,
    origin: entryOrigin === 'auto' || entryOrigin === 'manager' ? entryOrigin : null,
    turn_id: asString(record.turn_id) || null,
    question: asString(record.question),
    content: normalizedContent,
    created_at: asString(record.created_at),
    model_meta: asRecord(record.model_meta),
  }
}

export type ManagerFollowupItem = {
  item_id: string
  concern_or_scenario: string
  basis_status: 'confirmed' | 'inferred' | 'generic'
  evidence_summary: string
  followup_type: 'video' | 'article' | 'checklist' | 'email' | 'case' | 'news' | 'useful_tip' | 'other'
  idea: string
  why_it_may_help: string
  suggested_channel: string
  timing: string
  target_micro_conversion: string
  caution: string
}

export type ManagerFollowupsRecord = {
  id: number
  content: { followups_contract: 'followup_plan_v1'; context_summary: string; items: ManagerFollowupItem[] }
  created_at: string
}

export type ManagerFollowupsJob = {
  job_id: string
  deal_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  stage: 'queued' | 'context' | 'llm' | 'saving' | 'done' | 'error'
  detail: string
  percent: number
  followups_id?: number | null
  reused?: boolean
  error?: string | null
}

export function startManagerFullScript(
  dealId: string,
  quickHelpId: number,
  selectedStrategy: ManagerQuickHelpStrategy,
  scriptMode: ManagerFullScriptMode,
  confirmPaid = true,
) {
  return api<ManagerFullScriptJob>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/full-script`, {
    method: 'POST',
    body: JSON.stringify({ quick_help_id: quickHelpId, selected_strategy: selectedStrategy, script_mode: scriptMode, confirm_paid: confirmPaid }),
  })
}

export function fetchManagerFullScriptJob(jobId: string) {
  return api<ManagerFullScriptJob>(`/api/deal-control/full-script-jobs/${encodeURIComponent(jobId)}`)
}

export function fetchManagerFullScript(
  dealId: string,
  quickHelpId: number,
  selectedStrategy: ManagerQuickHelpStrategy,
  scriptMode: ManagerFullScriptMode,
) {
  const query = new URLSearchParams({ quick_help_id: String(quickHelpId), selected_strategy: selectedStrategy, script_mode: scriptMode })
  return api<ManagerFullScriptWorkspace>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/full-script?${query.toString()}`,
  )
}

export function startManagerFollowups(dealId: string, confirmPaid = true) {
  return api<ManagerFollowupsJob>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/followups`, {
    method: 'POST', body: JSON.stringify({ confirm_paid: confirmPaid }),
  })
}

export function fetchManagerFollowupsJob(jobId: string) {
  return api<ManagerFollowupsJob>(`/api/deal-control/followup-jobs/${encodeURIComponent(jobId)}`)
}

export function fetchManagerFollowups(dealId: string) {
  return api<{ followups: ManagerFollowupsRecord | null }>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/followups`)
}

export type ManagerCompanionLastContact = {
  event_id: string
  channel?: string | null
  direction?: string | null
  occurred_at?: string | null
  duration_seconds?: number | null
  subject?: string | null
  contact_class?: string | null
  content_available?: boolean
}

export type ManagerCompanionRecord = {
  id: number
  last_event_id?: string
  content: {
    companion_contract: 'companion_message_v1'
    understood: string[]
    message_text: string
    insufficient_reason?: string | null
  }
  created_at: string
}

export type ManagerCompanionJob = {
  job_id: string
  deal_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  stage: string
  detail: string
  percent: number
  companion_id?: number | null
  reused?: boolean
  analysis_started?: boolean
  analysis_decision?: string | null
  missing_reason?: string | null
  error?: string | null
}

export type ManagerCompanionWorkspace = {
  last_contact: ManagerCompanionLastContact | null
  companion: ManagerCompanionRecord | null
  source_report_id?: number | null
}

export function startManagerCompanion(dealId: string, confirmPaid = true, regenerate = false, managerNote = '') {
  return api<ManagerCompanionJob>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/companion`, {
    method: 'POST', body: JSON.stringify({ confirm_paid: confirmPaid, regenerate, manager_note: managerNote }),
  })
}

export function fetchManagerCompanionJob(jobId: string) {
  return api<ManagerCompanionJob>(`/api/deal-control/companion-jobs/${encodeURIComponent(jobId)}`)
}

export function fetchManagerCompanion(dealId: string) {
  return api<ManagerCompanionWorkspace>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/companion`)
}

export async function transcribeManagerVoice(
  dealId: string,
  audio: Blob,
  confirmPaid = true,
) {
  const body = new FormData()
  const extension = audio.type.includes('ogg') ? 'ogg' : audio.type.includes('mp4') ? 'mp4' : 'webm'
  body.append('audio', audio, `manager-voice-${encodeURIComponent(dealId)}.${extension}`)
  body.append('deal_id', dealId)
  body.append('confirm_paid', String(confirmPaid))
  body.append('language', 'ru')
  return api<{ text: string }>('/api/deal-control/voice/transcribe', {
    method: 'POST',
    body,
  })
}

export async function fetchManagerAssistantWorkspace(dealId: string) {
  const payload = await api<ManagerAssistantWorkspace>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/assistant-workspace`,
  )
  const entries = (Array.isArray(payload.entries) ? payload.entries : [])
    .map(normalizeManagerQuickHelpEntry)
    .filter((entry): entry is ManagerQuickHelpEntry => Boolean(entry))
  const currentByMode = {
    push: payload.current_by_mode?.push
      ? normalizeManagerQuickHelpEntry(payload.current_by_mode.push)
      : entries.find((entry) => (entry.mode || 'reanimator') === 'push') || null,
    reanimator: payload.current_by_mode?.reanimator
      ? normalizeManagerQuickHelpEntry(payload.current_by_mode.reanimator)
      : entries.find((entry) => (entry.mode || 'reanimator') === 'reanimator') || null,
  }
  return {
    ...payload,
    entries,
    current_by_mode: currentByMode,
  }
}

export function recordManagerCommunicationCompleted(dealId: string, quickHelpId: number) {
  return api<{ ok: boolean }>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/assistant/communication-completed`,
    { method: 'POST', body: JSON.stringify({ quick_help_id: quickHelpId }) },
  )
}

export function recordRecommendationEvent(
  dealId: string,
  eventType: 'shown' | 'viewed',
  recommendationKind: 'deal_task' | 'quick_help',
  recommendationId: number,
) {
  const occurrenceId = eventType === 'viewed'
    ? (globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`)
    : undefined
  return api<{ ok: boolean; event_id: number }>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/recommendation-events`,
    {
      method: 'POST',
      body: JSON.stringify({
        event_type: eventType,
        recommendation_kind: recommendationKind,
        recommendation_id: recommendationId,
        occurrence_id: occurrenceId,
      }),
    },
  )
}

export function recordQuickHelpOpened(
  dealId: string,
  assistantMode?: ManagerAssistantMode | null,
  activeQuickHelpId?: number | null,
) {
  const occurrenceId = globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return api<{ ok: boolean; event_id: number }>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/quick-help-opened`,
    {
      method: 'POST',
      body: JSON.stringify({
        occurrence_id: occurrenceId,
        entrypoint: 'assistant_button',
        assistant_mode: assistantMode ?? null,
        active_quick_help_id: activeQuickHelpId ?? null,
      }),
    },
  )
}

export function updateDealContextLeverPriority(
  dealId: string,
  leverId: string,
  priority: 1 | 2 | 3 | null,
) {
  return api<{
    ok: boolean
    priorities: Array<{ lever_id: string; priority: 1 | 2 | 3 | null }>
  }>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/context/levers/${encodeURIComponent(leverId)}/priority`,
    { method: 'PUT', body: JSON.stringify({ priority }) },
  )
}

export function syncDealControl() {
  return api<DealControlDashboard>('/api/deal-control/sync', { method: 'POST' }).then(normalizeDealControlDashboard)
}

export function saveDealControlScope(body: { initial_deal_ids: string[]; manager_ids: string[]; pipeline_id?: string; pipeline_ids?: string[] }) {
  return api<{ ok: boolean; scope: DealControlDashboard['scope'] }>('/api/deal-control/scope', {
    method: 'PUT', body: JSON.stringify(body),
  })
}

export function updateDealControlDeal(dealId: string, body: {
  probability: number | null
  expected_payment_period: string | null
  next_control_at: string | null
}) {
  return api<DealControlDeal>(`/api/deal-control/deals/${encodeURIComponent(dealId)}`, {
    method: 'PUT', body: JSON.stringify(body),
  })
}

export function createDealControlTask(dealId: string, body: {
  task_text: string
  touch_type?: string | null
  expected_result?: string | null
  due_at: string
}) {
  return api<DealControlTask>(`/api/deal-control/deals/${encodeURIComponent(dealId)}/tasks`, {
    method: 'POST', body: JSON.stringify(body),
  })
}

export function updateDealControlTask(taskId: number, body: Partial<Pick<DealControlTask,
  'task_text' | 'touch_type' | 'expected_result' | 'due_at' | 'local_status' | 'business_result_status' | 'business_result_note'
>> & { reschedule_reason?: string | null }) {
  return api<DealControlTask>(`/api/deal-control/tasks/${taskId}`, {
    method: 'PUT', body: JSON.stringify(body),
  })
}

export function updateDealControlBitrixTaskCompletion(
  dealId: string,
  activityId: string,
  completed: boolean,
) {
  return api<{ ok: boolean; state: {
    activity_id: string
    deal_id: string
    local_completed: boolean
    local_completed_at?: string | null
    local_completed_by?: 'manager' | 'rop' | null
  } }>(`/api/deal-control/bitrix-tasks/${encodeURIComponent(activityId)}/completion`, {
    method: 'PUT',
    body: JSON.stringify({ deal_id: dealId, completed }),
  })
}

export function updateDealControlChecklistItemCompletion(
  dealId: string,
  itemId: string,
  completed: boolean,
) {
  return api<{ ok: boolean; checklist: DealControlChecklist }>(
    `/api/deal-control/deals/${encodeURIComponent(dealId)}/checklist/${encodeURIComponent(itemId)}/completion`,
    { method: 'PUT', body: JSON.stringify({ completed }) },
  )
}

export function fetchDealControlMetrics() {
  return api<DealControlMetrics>('/api/deal-control/metrics')
}

export function confirmDealControlTaskCrmMatch(taskId: number) {
  return api<DealControlTask>(`/api/deal-control/tasks/${taskId}/confirm-crm-match`, { method: 'POST' })
}

export function saveDealControlTaskOutcome(taskId: number, body: {
  contact_status: DealControlTaskOutcome['contact_status']
  result_status: DealControlTaskOutcome['result_status']
  result_note?: string | null
  next_step_text?: string | null
  next_step_at?: string | null
  evidence_kind?: DealControlTaskOutcome['evidence_kind']
  evidence_id?: string | null
}) {
  return api<DealControlTaskOutcome>(`/api/deal-control/tasks/${taskId}/outcomes`, {
    method: 'POST', body: JSON.stringify(body),
  })
}

export function reviewDealControlCrmFact(taskId: number, factId: number, body: {
  review_status: 'confirmed' | 'rejected'
  contact_class?: DealControlCrmFact['contact_class'] | null
}) {
  return api<DealControlCrmFact>(`/api/deal-control/tasks/${taskId}/crm-facts/${factId}/review`, {
    method: 'POST', body: JSON.stringify(body),
  })
}

export function recordDealControlTaskEvent(
  taskId: number,
  eventType: 'guidance_opened' | 'guidance_copied',
  eventKey?: string | null,
) {
  return api<{ ok: boolean }>(`/api/deal-control/tasks/${taskId}/events`, {
    method: 'POST',
    body: JSON.stringify({ event_type: eventType, event_key: eventKey || null }),
  })
}

export function startDealTaskGuidance(taskId: number) {
  return api<DealTaskGuidanceJob>(`/api/deal-control/tasks/${taskId}/guidance`, {
    method: 'POST',
    body: JSON.stringify({ confirm_paid: true }),
  })
}

export function fetchDealTaskGuidanceJob(jobId: string) {
  return api<DealTaskGuidanceJob>(`/api/deal-control/guidance-jobs/${encodeURIComponent(jobId)}`)
}

export function fetchDailyControlHistory() {
  return api<DailyControlHistory>('/api/daily-control/reports')
}

export function fetchDailyControlReport(reportId: number) {
  return api<DailyControlReport>(`/api/daily-control/reports/${reportId}`)
}

export function startDailyControlReport() {
  return api<DailyControlGeneration>('/api/daily-control/reports', { method: 'POST' })
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

export type AutomaticAnalysisLatest = {
  business_date: string | null
  status: string
  processed: number
  total: number
  succeeded: number
  errors: number
  skipped: number
  full: number
  mini: number
  reports_published: number
  current_stage: string | null
  current?: { title: string; stage: string | null } | null
  started_at: string | null
  updated_at: string | null
  finished_at: string | null
}

export function fetchAutomaticAnalysisLatest() {
  return api<{ latest: AutomaticAnalysisLatest | null }>('/api/automatic-analysis/latest')
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
