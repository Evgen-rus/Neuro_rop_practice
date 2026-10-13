export type LeadControlGroup = 'active' | 'closed'

export const MAX_LEAD_CONTROL_SELECTION = 10

const CLOSURE_VERDICT: Record<string, string> = {
  confirmed_correct: 'Закрытие подтверждено',
  disputed: 'Закрытие оспорено',
  insufficient_evidence: 'Недостаточно данных для оценки',
  not_applicable: 'Оценка не применима',
}

export function leadClosureLabel(group: LeadControlGroup, verdict: string | null | undefined): string | null {
  if (group !== 'closed' || !verdict) return null
  return CLOSURE_VERDICT[verdict] || verdict
}

export function leadCategoryLabel(category: string | null | undefined): string {
  const value = category?.trim()
  return value && value.toLowerCase() !== 'unknown' ? value : 'Не определена'
}

export function leadTechnicalSummary(qualification: {
  regulation_version?: string | null
  technical_collected_count?: number | null
  technical_total_count?: number | null
  technical_partial_count?: number | null
  technical_readiness?: string | null
  technical_required_now?: boolean | null
} | null | undefined): string {
  const hasCounts = typeof qualification?.technical_collected_count === 'number'
    && typeof qualification.technical_total_count === 'number'
  const summary = hasCounts
    ? `ТЗ собрано ${qualification.technical_collected_count} из ${qualification.technical_total_count}${qualification.technical_partial_count ? ` · частично ${qualification.technical_partial_count}` : ''}`
    : qualification?.regulation_version
      ? 'Сводка ТЗ недоступна'
      : qualification
        ? 'ТЗ не сохранено в старом отчёте'
        : 'Оценка ТЗ отсутствует'
  const equipment = qualification?.technical_readiness === 'unknown' ? 'тип оборудования не определён' : ''
  const required = qualification?.technical_required_now === true
    ? 'требуется сейчас'
    : qualification?.technical_required_now === false
      ? 'пока не требуется'
      : 'не указано, требуется ли сейчас'
  return [summary, equipment, required].filter(Boolean).join(' · ')
}

export function buildLeadControlSearchRequest(group: LeadControlGroup, managerId: string, refreshCrm = false) {
  return {
    entity_type: 'lead' as const,
    created_days: 30,
    modified_days: 0,
    limit: 100,
    priority: null,
    pipeline_ids: [],
    stage_ids: [],
    lead_control_group: group,
    manager_id: managerId || undefined,
    refresh_crm: refreshCrm,
    review_view: 'all' as const,
    save: false,
  }
}

export function toggleLeadSelection(selectedIds: string[], id: string): string[] {
  if (selectedIds.includes(id)) return selectedIds.filter((item) => item !== id)
  if (selectedIds.length >= MAX_LEAD_CONTROL_SELECTION) return selectedIds
  return [...selectedIds, id]
}
