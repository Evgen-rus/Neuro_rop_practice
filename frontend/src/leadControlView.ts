export type LeadControlGroup = 'active' | 'closed'

export const MAX_LEAD_CONTROL_SELECTION = 10

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
