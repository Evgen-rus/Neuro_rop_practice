import type { DailyControlCreationKind, DailyControlDeal } from './api'

export type DailyControlTimeFilter = 'all' | 'today' | 'tomorrow' | 'future'
export const DEFAULT_TIME_FILTER: DailyControlTimeFilter = 'all'
const CLIENT_CONTACT_KINDS = new Set(['call', 'message'])

export function dealMatchesTime(deal: DailyControlDeal, filter: DailyControlTimeFilter) {
  if (filter === 'all') return true
  const scope = deal.day_scope
  if (!scope) return false
  const buckets = scope.task_buckets
  const contact = (scope.activity_kinds || []).some((kind) => CLIENT_CONTACT_KINDS.has(kind))
  // Фильтры режут только сохранённый дневной набор, не весь портфель Deal Control.
  if (filter === 'today') {
    return buckets.includes('today') || buckets.includes('overdue') || Boolean(scope.had_day_obligation) || contact
  }
  if (filter === 'tomorrow') return buckets.includes('tomorrow')
  return buckets.includes('future') || buckets.includes('unscheduled')
}

export function canFilterReport(deals: DailyControlDeal[]) {
  return deals.some((deal) => Boolean(deal.day_scope))
}

export function hasReportDayWork(deal: DailyControlDeal) {
  if (deal.day_scope) {
    return (deal.day_scope.activity_kinds || []).some((kind) => CLIENT_CONTACT_KINDS.has(kind))
  }
  return Number(deal.communications_today?.completed || 0) > 0
}

export function reportDayLabels(deal: DailyControlDeal): Array<{ kind: 'due' | 'work' | 'untouched'; text: string }> {
  const scope = deal.day_scope
  if (!scope) return []
  const labels: Array<{ kind: 'due' | 'work' | 'untouched'; text: string }> = []
  if (scope.untouched) labels.push({ kind: 'untouched', text: 'Не дожали' })
  if (scope.task_buckets.includes('today') || (scope.had_day_obligation && !scope.task_buckets.includes('overdue'))) {
    labels.push({ kind: 'due', text: 'Задача на этот день' })
  }
  if (scope.task_buckets.includes('overdue')) labels.push({ kind: 'due', text: 'Просрочена к срезу' })
  const workLabels: Record<string, string> = {
    call: 'Были звонки',
    message: 'Была переписка',
    bitrix_task_completed: 'Задача завершена в CRM',
    bitrix_task_rescheduled: 'Задача перенесена',
  }
  for (const kind of scope.activity_kinds) {
    if (workLabels[kind]) labels.push({ kind: 'work', text: workLabels[kind] })
  }
  return labels
}

export function reportHeading(report: {
  heading?: string | null
  creation_kind?: DailyControlCreationKind | string | null
  business_date?: string | null
  cutoff_at?: string | null
}) {
  if (report.heading) return report.heading
  const dateMatch = String(report.business_date || report.cutoff_at || '').match(/(\d{4})-(\d{2})-(\d{2})/)
  const timeMatch = String(report.cutoff_at || '').match(/T(\d{2}):(\d{2})/)
  if (!dateMatch) return 'Ежедневный контроль'
  const stamp = timeMatch ? `${dateMatch[3]}.${dateMatch[2]} ${timeMatch[1]}:${timeMatch[2]}` : `${dateMatch[3]}.${dateMatch[2]}`
  if (report.creation_kind === 'automatic_planning') return `ОТЧЕТ К ПЛАНЕРКЕ ${stamp}`
  if (report.creation_kind === 'automatic_day_end') return `ОТЧЕТ ФИНАЛЬНЫЙ ЗА ${stamp}`
  if (report.creation_kind === 'manual') return `ОТЧЕТ ВРУЧНУЮ ${stamp}`
  return `Ежедневный контроль ${stamp}`
}

export function shouldOpenLatestReport(currentId: number | undefined, latestId: number | null, reviewStarted: boolean, historyPinned: boolean) {
  return Boolean(latestId && latestId !== currentId && !reviewStarted && !historyPinned)
}

export function firstReviewDeal(deals: DailyControlDeal[], managerId: string | null | undefined) {
  const own = deals.filter((deal) => String(deal.manager_id || '') === String(managerId || ''))
  return own.find((deal) => deal.status === 'red') || own[0]
}

export function dailyTaskTotals(deals: DailyControlDeal[]) {
  const tasks = deals.flatMap((deal) => deal.task_results || [])
  return {
    tasks_completed: new Set(tasks.filter((task) => task.completed_today).map((task) => task.key)).size,
    tasks_rescheduled: new Set(tasks.filter((task) => task.reschedules.length).map((task) => task.key)).size,
  }
}

export function matchesDailySearch(deal: DailyControlDeal, search: string) {
  const needle = search.trim().toLocaleLowerCase('ru')
  return !needle || [deal.deal_id, deal.title, ...(deal.task_results || []).map((task) => task.subject)]
    .some((value) => String(value || '').toLocaleLowerCase('ru').includes(needle))
}
