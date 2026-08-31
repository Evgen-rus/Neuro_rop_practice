import type { DailyControlCreationKind, DailyControlDeal, DailyTaskResult } from './api'
import { formatMoscowDateTime, moscowDateInputValue, parseMoscowDateTime } from './dateTime.ts'

export type DailyControlTimeFilter = 'all' | 'today' | 'tomorrow' | 'future'
export const DEFAULT_TIME_FILTER: DailyControlTimeFilter = 'all'
const CLIENT_CONTACT_KINDS = new Set(['call', 'message'])

const NO_DAY_CONTACT_LABEL = 'В этот день коммуникаций нет'

// Слепок хранит «сегодня» как день отчёта. На экране это календарный «сегодня» понедельника.
const SNAPSHOT_DAY_PHRASES: Array<[string, string]> = [
  [
    'Сегодня по актуальной задаче ещё нет содержательной клиентской коммуникации',
    'В этот день по актуальной задаче не было содержательной клиентской коммуникации',
  ],
  [
    'Нет данных: сегодняшние клиентские коммуникации получены не полностью.',
    'Нет данных: клиентские коммуникации этого дня получены не полностью.',
  ],
  [
    'Нет данных: результат сегодняшнего звонка ещё не подтверждён.',
    'Нет данных: результат звонка этого дня не подтверждён.',
  ],
  [
    'На сегодня нет актуальной задачи или контрольной точки; содержательной коммуникации пока нет.',
    'На этот день нет актуальной задачи или контрольной точки; содержательной коммуникации не было.',
  ],
  [
    'AI не подтвердил содержательную работу в сегодняшних коммуникациях.',
    'AI не подтвердил содержательную работу в коммуникациях этого дня.',
  ],
  ['Ожидает корректной AI-оценки сегодняшней работы.', 'Ожидает корректной AI-оценки работы этого дня.'],
  ['На сегодня нет актуальной задачи', 'На этот день нет актуальной задачи'],
  ['Сегодня есть клиентская коммуникация', 'В этот день есть клиентская коммуникация'],
  ['Сегодня не подтверждено', 'В этот день не подтверждено'],
  ['Сегодня не требуется', 'В этот день не требуется'],
  ['Сегодня коммуникаций нет', NO_DAY_CONTACT_LABEL],
  ['Коммуникации за сегодня', 'Коммуникации за этот день'],
]

export function snapshotDayText(value?: string | null): string {
  if (!value) return value || ''
  let text = value
  for (const [from, to] of SNAPSHOT_DAY_PHRASES) {
    if (text.includes(from)) text = text.split(from).join(to)
  }
  return text
}

export function dailyQualityCaption(quality: DailyControlDeal['quality'], snapshotDay = false) {
  if (quality.status === 'pending_analysis') return 'Ожидает AI-оценки'
  if (quality.status === 'not_required') return snapshotDay ? 'В этот день не требуется' : 'Сегодня не требуется'
  if (quality.status === 'no_work') return '0 из 3 · работа не подтверждена'
  if (quality.status === 'assessed' && quality.confirmed_count != null) {
    return `${quality.confirmed_count} из ${quality.total} · AI-оценка`
  }
  return 'Нет данных'
}

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

function validStamp(value?: string | null): number | null {
  if (!value) return null
  const stamp = parseMoscowDateTime(value).getTime()
  return Number.isFinite(stamp) ? stamp : null
}

export function communicationDayLabels(deal: DailyControlDeal, cutoffAt?: string): string[] {
  const communications = deal.communications_today
  const cutoff = cutoffAt || deal.day_scope?.cutoff_at
  const cutoffStamp = validStamp(cutoff)
  const day = cutoffStamp !== null ? moscowDateInputValue(cutoffStamp) : deal.day_scope?.business_date
  if (!communications?.available || communications.unavailable || (day && communications.date !== day)) {
    return ['Данные о коммуникациях недоступны']
  }
  const sourceItems = communications.items || []
  const items = sourceItems.filter((item) => {
    const stamp = validStamp(item.occurred_at)
    return stamp !== null && (!day || moscowDateInputValue(stamp) === day)
      && (cutoffStamp === null || stamp <= cutoffStamp)
  })
  const calls = items.filter((item) => item.channel === 'call')
  const messages = items.filter((item) => ['email', 'message', 'whatsapp', 'telegram', 'max', 'sms'].includes(item.channel))
  // Old snapshots may only have counters. Never infer a result or direction from them.
  const legacy = sourceItems.length === 0
  const callCount = legacy ? (communications.calls_total ?? communications.calls ?? 0) : calls.length
  const messageCount = legacy ? (communications.messages || 0) : messages.length
  const labels: string[] = []
  if (callCount) {
    const attempts = calls.filter((item) => item.direction === 'outgoing' && item.call_outcome === 'no_answer').length
    const connected = calls.some((item) => item.call_outcome === 'connected')
    // contact_class=attempt is a default for calls, not evidence of a missed call.
    // Neither connection, duration nor a transcript proves a substantive conversation.
    if (!legacy && attempts === callCount) labels.push(callCount === 1 ? 'Была попытка связи' : 'Были попытки связи')
    else if (connected) labels.push(callCount === 1 ? 'Был звонок' : 'Были звонки')
    else labels.push(callCount === 1 ? 'Был звонок · результат не определён' : 'Были звонки · результат не определён')
  }
  if (messageCount) {
    const incoming = messages.some((item) => item.direction === 'incoming')
    const outgoing = messages.some((item) => item.direction === 'outgoing')
    const unknown = messages.some((item) => !['incoming', 'outgoing'].includes(item.direction))
    if (incoming && outgoing) labels.push('Была переписка')
    else if (legacy || unknown) labels.push(messageCount === 1 ? 'Было сообщение' : 'Были сообщения')
    else if (incoming) labels.push(messageCount === 1 ? 'Получено сообщение клиента' : 'Получены сообщения клиента')
    else labels.push(messageCount === 1 ? 'Отправлено сообщение' : 'Отправлены сообщения')
  }
  return labels.length ? labels : [NO_DAY_CONTACT_LABEL]
}

type DayLabel = { kind: 'due' | 'work' | 'rescheduled' | 'no-contact' | 'unavailable'; text: string }

export function reportDayLabels(deal: DailyControlDeal, cutoffAt?: string): DayLabel[] {
  const scope = deal.day_scope
  const labels: DayLabel[] = []
  if (scope && (scope.task_buckets.includes('today') || (scope.had_day_obligation && !scope.task_buckets.includes('overdue')))) {
    labels.push({ kind: 'due', text: 'Задача на этот день' })
  }
  if (scope?.task_buckets.includes('overdue')) labels.push({ kind: 'due', text: 'Просрочена к срезу' })
  labels.push(...communicationDayLabels(deal, cutoffAt).map((text): DayLabel => ({
    kind: text === NO_DAY_CONTACT_LABEL ? 'no-contact'
      : text === 'Данные о коммуникациях недоступны' ? 'unavailable' : 'work',
    text,
  })))
  const activityLabels: Record<string, DayLabel> = {
    bitrix_task_completed: { kind: 'work', text: 'Задача завершена в CRM' },
    bitrix_task_rescheduled: { kind: 'rescheduled', text: 'Задача перенесена' },
  }
  for (const kind of scope?.activity_kinds || []) {
    if (activityLabels[kind]) labels.push(activityLabels[kind])
  }
  return labels
}

export type TaskStripKind = 'overdue' | 'completed' | 'rescheduled' | 'waiting' | 'unknown' | 'empty'

export function taskStripStatus(task: { status?: string; overdue?: boolean; reschedules?: unknown[] }): { kind: TaskStripKind; text: string } {
  if (task.status === 'completed') return { kind: 'completed', text: 'выполнена' }
  if (task.overdue) return { kind: 'overdue', text: 'просрочена' }
  if ((task.reschedules || []).length) return { kind: 'rescheduled', text: 'перенесена' }
  if (task.status === 'open') return { kind: 'waiting', text: 'ждет выполнения' }
  return { kind: 'unknown', text: 'статус не сохранён' }
}

type TaskStripTask = Partial<Pick<DailyTaskResult, 'key' | 'status' | 'deadline' | 'completed_at' | 'overdue' | 'reschedules'>>

function taskDate(value?: string | null, cutoffAt?: string): string | null {
  const stamp = validStamp(value)
  if (stamp === null) return null
  const cutoff = validStamp(cutoffAt)
  const time = formatMoscowDateTime(stamp, { hour: '2-digit', minute: '2-digit' })
  const otherYear = cutoff === null || moscowDateInputValue(stamp).slice(0, 4) !== moscowDateInputValue(cutoff).slice(0, 4)
  const date = formatMoscowDateTime(stamp, { day: 'numeric', month: 'long', ...(otherYear ? { year: 'numeric' as const } : {}) })
  if (!date || !time) return null
  return `${date}, ${time}`
}

export function taskDeadlineLabel(task: TaskStripTask, cutoffAt?: string): { kind: TaskStripKind; text: string } {
  const date = taskDate(task.deadline, cutoffAt)
  if (task.status === 'completed') {
    const completed = taskDate(task.completed_at, cutoffAt)
    return { kind: 'completed', text: completed ? `Задача выполнена ${completed}` : 'Задача выполнена · время не сохранено' }
  }
  if (task.status !== 'open') return { kind: 'unknown', text: `Задача: статус не сохранён${date ? ` · срок ${date}` : ''}` }
  if (task.overdue) return { kind: 'overdue', text: `Задача просрочена${date ? ` · срок ${date}` : ''}` }
  if (!task.deadline) return { kind: 'waiting', text: 'Задача без срока' }
  if (!date) return { kind: 'unknown', text: 'Задача: срок не определён' }
  if (task.reschedules?.length) {
    return { kind: 'rescheduled', text: `Задача перенесена на ${date}` }
  }
  return { kind: 'waiting', text: `Задача на ${date}` }
}

export function sortDayTasks<T extends TaskStripTask>(tasks: T[]): T[] {
  const rank = (task: TaskStripTask) => task.status === 'open' ? (task.overdue ? 0 : 1) : task.status === 'completed' ? 3 : 2
  return [...tasks].sort((left, right) => rank(left) - rank(right)
    || (validStamp(left.deadline) ?? Infinity) - (validStamp(right.deadline) ?? Infinity)
    || String(left.key || '').localeCompare(String(right.key || '')))
}

export function tasksStripSummary(tasks?: TaskStripTask[], cutoffAt?: string): { kind: TaskStripKind; text: string } {
  if (!tasks) return { kind: 'unknown', text: 'Подробности задач в этом старом срезе не сохранялись' }
  if (!tasks.length) return { kind: 'empty', text: 'Открытых задач нет' }
  const first = sortDayTasks(tasks)[0]
  const summary = taskDeadlineLabel(first, cutoffAt)
  return tasks.length === 1 ? summary : { ...summary, text: `Задач: ${tasks.length} · ${summary.text}` }
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
  const year = Number(dateMatch[1])
  const month = Number(dateMatch[2])
  const day = Number(dateMatch[3])
  const date = new Date(Date.UTC(year, month - 1, day))
  const weekdays = ['воскресенье', 'понедельник', 'вторник', 'среду', 'четверг', 'пятницу', 'субботу']
  const months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
  const stamp = `${weekdays[date.getUTCDay()]}, ${day} ${months[month - 1]} ${year}`
  const cutoff = timeMatch ? `${timeMatch[1]}:${timeMatch[2]} МСК` : 'время не указано'
  if (report.creation_kind === 'automatic_planning') return `Состояние команды на ${stamp} — срез на ${cutoff}`
  if (report.creation_kind === 'automatic_day_end') return `Итог команды за ${stamp} — срез на ${cutoff}`
  if (report.creation_kind === 'manual') return `Ручной слепок за ${stamp} — на ${cutoff}`
  return `Ежедневный контроль за ${stamp} — срез на ${cutoff}`
}

const ROUTINE_TECHNICAL_WARNING_PREFIXES = [
  'Завершение автоматического пакета за сегодня не подтверждено.',
  'На момент создания отчёта автоматический пакет ещё выполнялся.',
]

export function businessReportWarnings(warnings: readonly string[] | null | undefined): string[] {
  return (warnings || []).filter((warning) =>
    !ROUTINE_TECHNICAL_WARNING_PREFIXES.some((prefix) => warning.startsWith(prefix)),
  )
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
