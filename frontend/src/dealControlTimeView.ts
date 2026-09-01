import type { DealControlBitrixTask, DealControlDeal, DealControlDashboard } from './api'
import { rescheduleFromDueOnOrBeforeToday, rescheduleFromDueToday } from './taskReschedules.ts'

export type DealControlTimeView = 'all' | 'attention' | 'today' | 'tomorrow' | 'future' | 'overdue'

type TimeDeal = Pick<DealControlDeal, 'primary_bitrix_task' | 'bitrix_tasks'>

export function primaryBitrixTaskOf(deal: TimeDeal) {
  return deal.primary_bitrix_task || deal.bitrix_tasks?.[0] || null
}

export function controlTimeBucket(deal: TimeDeal): string {
  return primaryBitrixTaskOf(deal)?.time_bucket || 'missing'
}

function taskHeldInRopToday(task: DealControlBitrixTask, now?: Date | string): boolean {
  const result = task.day_result
  if (!result?.reschedules?.length) return false
  // Сервер уже помечает was_due, если текущий или прежний срок был сегодня/раньше.
  if (result.was_due) return true
  return result.reschedules.some((change) => rescheduleFromDueOnOrBeforeToday(change.from_deadline, now))
}

function taskHeldFromToday(task: DealControlBitrixTask, now?: Date | string): boolean {
  return Boolean(task.day_result?.reschedules?.some((change) => rescheduleFromDueToday(change.from_deadline, now)))
}

export function dealHeldInRopToday(deal: TimeDeal, now?: Date | string): boolean {
  return (deal.bitrix_tasks || []).some((task) => taskHeldInRopToday(task, now))
}

export function dealMatchesTime(
  deal: TimeDeal,
  view: DealControlTimeView,
  options: { keepRescheduledInToday?: boolean; now?: Date | string } = {},
) {
  if (view === 'all') return true
  const bucket = controlTimeBucket(deal)
  const held = Boolean(options.keepRescheduledInToday && dealHeldInRopToday(deal, options.now))
  if (view === 'attention') return bucket === 'missing' || bucket === 'overdue'
  if (view === 'today') return bucket === 'missing' || bucket === 'today' || bucket === 'overdue' || held
  if (view === 'tomorrow') return bucket === 'tomorrow' && !held
  if (view === 'future') return (bucket === 'future' || bucket === 'unscheduled') && !held
  return bucket === view
}

export function ropTaskKpis(deals: TimeDeal[], now?: Date | string): Pick<
  DealControlDashboard['summary'],
  | 'tasks_today'
  | 'tasks_tomorrow'
  | 'tasks_future'
  | 'tasks_overdue'
  | 'tasks_missing'
  | 'tasks_plan_today'
  | 'tasks_completed_today'
  | 'tasks_rescheduled_today'
> {
  let missing = 0
  let overdue = 0
  let today = 0
  let tomorrow = 0
  let future = 0
  let extraHeld = 0
  let extraHeldFromToday = 0
  let completed = 0
  let rescheduled = 0
  for (const deal of deals) {
    const task = primaryBitrixTaskOf(deal)
    const held = dealHeldInRopToday(deal, now)
    const heldFromToday = (deal.bitrix_tasks || []).some((item) => taskHeldFromToday(item, now))
    rescheduled += (deal.bitrix_tasks || []).filter((item) => Boolean(item.day_result?.reschedules?.length)).length
    if (!task) {
      missing += 1
      continue
    }
    const bucket = task.time_bucket
    if (bucket === 'overdue') overdue += 1
    else if (bucket === 'today') today += 1
    else if (bucket === 'tomorrow') {
      if (held) extraHeld += 1
      else tomorrow += 1
    } else if (bucket === 'future' || bucket === 'unscheduled') {
      if (held) extraHeld += 1
      else future += 1
    }
    if (heldFromToday && bucket !== 'today') extraHeldFromToday += 1
    if (
      (['overdue', 'today'].includes(bucket) || held)
      && ['local', 'bitrix'].includes(task.completion_state)
    ) {
      completed += 1
    }
  }
  return {
    tasks_today: today + extraHeldFromToday,
    tasks_tomorrow: tomorrow,
    tasks_future: future,
    tasks_overdue: overdue,
    tasks_missing: missing,
    tasks_plan_today: missing + overdue + today + extraHeld,
    tasks_completed_today: completed,
    tasks_rescheduled_today: rescheduled,
  }
}
