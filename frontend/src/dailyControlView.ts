import type { DailyControlDeal } from './api'

export type DailyControlTimeFilter = 'all' | 'today' | 'tomorrow' | 'future'
export const DEFAULT_TIME_FILTER: DailyControlTimeFilter = 'today'
export function dealMatchesTime(deal: DailyControlDeal, filter: DailyControlTimeFilter) {
  if (filter === 'all') return true
  const scope = deal.day_scope
  if (!scope) return false
  const buckets = scope.task_buckets
  // Все даты уже вычислены сервером относительно сохранённого среза.
  if (filter === 'today') return buckets.includes('today') || buckets.includes('overdue') || scope.activity_kinds.length > 0
  if (filter === 'tomorrow') return buckets.includes('tomorrow')
  return buckets.includes('future') || buckets.includes('unscheduled')
}

export function canFilterReport(deals: DailyControlDeal[]) {
  return deals.some((deal) => Boolean(deal.day_scope))
}

export function hasReportDayWork(deal: DailyControlDeal) {
  return deal.day_scope ? deal.day_scope.activity_kinds.length > 0 : Number(deal.communications_today?.completed || 0) > 0
}

export function reportDayLabels(deal: DailyControlDeal): Array<{ kind: 'due' | 'work'; text: string }> {
  const scope = deal.day_scope
  if (!scope) return []
  const labels: Array<{ kind: 'due' | 'work'; text: string }> = []
  if (scope.task_buckets.includes('today')) labels.push({ kind: 'due', text: 'Задача на этот день' })
  if (scope.task_buckets.includes('overdue')) labels.push({ kind: 'due', text: 'Просрочена к срезу' })
  const workLabels = {
    call: 'Были звонки',
    message: 'Была переписка',
    stage_change: 'Изменена стадия',
    comment: 'Добавлен комментарий',
    bitrix_task_completed: 'Задача завершена в CRM',
    local_task_completed: 'Задача отмечена выполненной в НейроРОПе',
    checklist_completed: 'Выполнены пункты чек-листа',
  }
  for (const kind of scope.activity_kinds) {
    if (workLabels[kind]) labels.push({ kind: 'work', text: workLabels[kind] })
  }
  return labels
}

export function shouldOpenLatestReport(currentId: number | undefined, latestId: number | null, reviewStarted: boolean, historyPinned: boolean) {
  return Boolean(latestId && latestId !== currentId && !reviewStarted && !historyPinned)
}

export function firstReviewDeal(deals: DailyControlDeal[], managerId: string | null | undefined) {
  const own = deals.filter((deal) => String(deal.manager_id || '') === String(managerId || ''))
  return own.find((deal) => deal.status === 'red') || own[0]
}
