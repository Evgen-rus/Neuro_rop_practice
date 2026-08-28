import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'
import { canFilterReport, communicationDayLabels, dailyTaskTotals, DEFAULT_TIME_FILTER, dealMatchesTime, firstReviewDeal, matchesDailySearch, reportDayLabels, reportHeading, shouldOpenLatestReport, sortDayTasks, taskDeadlineLabel, taskStripStatus, tasksStripSummary } from './dailyControlView.ts'

const deals = ['today', 'overdue', 'missing', 'tomorrow', 'future', 'unscheduled'].map((bucket, index) => ({
  deal_id: String(index), manager_id: '1', status: index === 3 ? 'red' : 'yellow', bitrix_task_time_bucket: bucket,
  day_scope: { business_date: '2026-08-27', cutoff_at: '2026-08-27T15:45:00+03:00', task_buckets: bucket === 'missing' ? [] : [bucket], activity_kinds: [], legacy: false },
}))

test('default view shows the saved report set; today keeps due tasks, not idle deals', () => {
  const filtered = deals.filter((deal) => dealMatchesTime(deal, DEFAULT_TIME_FILTER))
  assert.equal(filtered.length, deals.length)
  assert.deepEqual(deals.filter((deal) => dealMatchesTime(deal, 'today')).map((deal) => deal.bitrix_task_time_bucket), ['today', 'overdue'])
  assert.equal(firstReviewDeal(deals.filter((deal) => dealMatchesTime(deal, 'today')), '1').deal_id, '0')
  const tomorrow = deals.filter((deal) => dealMatchesTime(deal, 'tomorrow'))
  assert.equal(firstReviewDeal(tomorrow, '1').deal_id, '3')
  assert.equal(firstReviewDeal([], '1'), undefined)
  assert.equal(deals.filter((deal) => dealMatchesTime(deal, 'future')).length, 2)
  assert.equal(deals.filter((deal) => dealMatchesTime(deal, 'all')).length, deals.length)
})

test('historical day filtering uses the frozen scope independently of the browser date', () => {
  assert.equal(canFilterReport(deals), true)
  const legacy = { ...deals[0], day_scope: { ...deals[0].day_scope, legacy: true } }
  assert.equal(canFilterReport([legacy]), true)
  assert.equal(dealMatchesTime(legacy, 'today'), true)
  assert.equal(canFilterReport([{ deal_id: 'old-server' }]), false)
})

test('only client contact brings a future-task deal into the today slice', () => {
  for (const activity of ['call', 'message']) {
    const deal = { ...deals[4], day_scope: { ...deals[4].day_scope, activity_kinds: [activity] },
      communications_today: { date: '2026-08-27', available: true, calls: activity === 'call' ? 1 : 0, messages: activity === 'message' ? 1 : 0, items: [] } }
    assert.equal(dealMatchesTime(deal, 'today'), true, activity)
    assert.equal(dealMatchesTime(deal, 'future'), true)
    assert.equal(reportDayLabels(deal).filter((item) => item.kind === 'work').length, 1)
  }
  for (const activity of ['stage_change', 'comment', 'bitrix_task_completed', 'bitrix_task_rescheduled', 'local_task_completed', 'checklist_completed']) {
    const deal = { ...deals[4], day_scope: { ...deals[4].day_scope, activity_kinds: [activity] } }
    assert.equal(dealMatchesTime(deal, 'today'), false, activity)
  }
  const completed = { ...deals[2], day_scope: { ...deals[2].day_scope, activity_kinds: ['bitrix_task_completed'] } }
  assert.equal(dealMatchesTime(completed, 'today'), false)
  assert.deepEqual(reportDayLabels(deals[0]).filter((item) => item.kind === 'due'), [{ kind: 'due', text: 'Задача на этот день' }])
  assert.deepEqual(reportDayLabels(deals[1]).filter((item) => item.kind === 'due'), [{ kind: 'due', text: 'Просрочена к срезу' }])
  assert.deepEqual(reportDayLabels(deals[4]), [{ kind: 'unavailable', text: 'Данные о коммуникациях недоступны' }])
})

test('legacy daily checklist cannot affect report activity or task totals', () => {
  const baseline = deals[4]
  const legacy = { ...baseline, checklist: { completed: 5, total: 5, items: [{ completed: true }] } }
  assert.deepEqual(dailyTaskTotals([legacy]), dailyTaskTotals([baseline]))
  assert.deepEqual(reportDayLabels(legacy), reportDayLabels(baseline))
  assert.equal(dealMatchesTime(legacy, 'today'), false)
})

test('a completed primary task cannot hide a different open task due on the report day', () => {
  const deal = { ...deals[2], day_scope: { ...deals[2].day_scope, task_buckets: ['today', 'future'] } }
  assert.equal(dealMatchesTime(deal, 'today'), true)
  assert.equal(dealMatchesTime(deal, 'future'), true)
})

test('a task moved to tomorrow still belongs to today and search looks inside the selected report', () => {
  const deal = {
    ...deals[3],
    title: 'Поставка линии',
    day_scope: { ...deals[3].day_scope, had_day_obligation: true, activity_kinds: ['bitrix_task_rescheduled'] },
    task_results: [{ key: 'task:9', subject: 'Согласовать КП', completed_today: true, reschedules: [{ occurred_at: '2026-08-27T12:00:00+03:00' }] }],
  }
  assert.equal(dealMatchesTime(deal, 'today'), true)
  assert.equal(dealMatchesTime(deal, 'tomorrow'), true)
  assert.deepEqual(dailyTaskTotals([deal]), { tasks_completed: 1, tasks_rescheduled: 1 })
  assert.equal(matchesDailySearch(deal, '101'), false)
  assert.equal(matchesDailySearch(deal, 'поставка'), true)
  assert.equal(matchesDailySearch(deal, 'согласовать'), true)
})

test('untouched obligation stays in the report without a separate badge', () => {
  const deal = { ...deals[0], day_scope: { ...deals[0].day_scope, had_day_obligation: true, untouched: true } }
  assert.deepEqual(reportDayLabels(deal)[0], { kind: 'due', text: 'Задача на этот день' })
  assert.equal(reportDayLabels(deal).some((item) => item.text.includes('Не дожали')), false)
  assert.equal(dealMatchesTime(deal, 'today'), true)
})

test('report heading puts date and cutoff time in the title', () => {
  assert.equal(
    reportHeading({ creation_kind: 'automatic_planning', business_date: '2026-08-27', cutoff_at: '2026-08-27T15:45:00+03:00' }),
    'ОТЧЕТ К ПЛАНЕРКЕ 27.08 15:45',
  )
  assert.equal(
    reportHeading({ creation_kind: 'automatic_day_end', business_date: '2026-08-27', cutoff_at: '2026-08-27T23:00:00+03:00' }),
    'ОТЧЕТ ФИНАЛЬНЫЙ ЗА 27.08 23:00',
  )
  assert.equal(reportHeading({ heading: 'ОТЧЕТ К ПЛАНЕРКЕ 27.08 15:45' }), 'ОТЧЕТ К ПЛАНЕРКЕ 27.08 15:45')
})

test('background refresh opens a new report only outside an active review or chosen history', () => {
  assert.equal(shouldOpenLatestReport(10, 11, false, false), true)
  assert.equal(shouldOpenLatestReport(undefined, 11, false, false), true)
  assert.equal(shouldOpenLatestReport(10, 11, true, false), false)
  assert.equal(shouldOpenLatestReport(10, 11, false, true), false)
  assert.equal(shouldOpenLatestReport(10, 10, false, false), false)
  assert.equal(shouldOpenLatestReport(10, null, false, false), false)
})

test('open task without overdue or reschedule waits for completion', () => {
  assert.deepEqual(taskStripStatus({ status: 'completed', overdue: true, reschedules: [{}] }), { kind: 'completed', text: 'выполнена' })
  assert.deepEqual(taskStripStatus({ status: 'open', overdue: true }), { kind: 'overdue', text: 'просрочена' })
  assert.deepEqual(taskStripStatus({ status: 'open', reschedules: [{ occurred_at: '2026-08-27T12:00:00+03:00' }] }), { kind: 'rescheduled', text: 'перенесена' })
  assert.deepEqual(taskStripStatus({ status: 'open' }), { kind: 'waiting', text: 'ждет выполнения' })
  assert.deepEqual(tasksStripSummary([{ status: 'open' }, { status: 'completed' }]), { kind: 'waiting', text: 'Задач: 2 · Задача без срока' })
  assert.deepEqual(tasksStripSummary([{ status: 'open', overdue: true }, { status: 'open' }]), { kind: 'overdue', text: 'Задач: 2 · Задача просрочена' })
  assert.deepEqual(tasksStripSummary([]), { kind: 'empty', text: 'Открытых задач нет' })
})

const cutoff = '2026-08-28T11:52:22+03:00'
const event = (values = {}) => ({ event_id: 'call:1', channel: 'call', direction: 'outgoing', occurred_at: '2026-08-28T09:39:47+03:00', ...values })
const communicationDeal = (items, values = {}) => ({
  ...deals[4],
  day_scope: { ...deals[4].day_scope, business_date: '2026-08-28', cutoff_at: cutoff, activity_kinds: ['call'] },
  communications_today: { date: '2026-08-28', available: true, calls: 0, messages: 0, items, ...values },
})

test('call labels use observed outcomes, never default contact class or duration as substance', () => {
  for (const [items, expected] of [
    [[event({ call_outcome: 'no_answer' })], 'Была попытка связи'],
    [[event({ call_outcome: 'no_answer' }), event({ event_id: 'call:2', call_outcome: 'no_answer' })], 'Были попытки связи'],
    [[event({ contact_class: 'attempt', call_outcome: 'connected', duration_seconds: 47 })], 'Был звонок'],
    [[event({ call_outcome: 'no_answer' }), event({ call_outcome: 'connected' })], 'Были звонки'],
    [[event({ contact_class: 'confirmed_contact', call_outcome: 'unknown', content_available: true, duration_seconds: 600 })], 'Был звонок · результат не определён'],
    [[event({ direction: 'incoming', call_outcome: 'no_answer' })], 'Был звонок · результат не определён'],
  ]) assert.deepEqual(communicationDayLabels(communicationDeal(items)), [expected])
})

test('messages distinguish sent, received, reciprocal and unknown directions', () => {
  const message = (direction, channel = 'message') => event({ channel, direction })
  for (const [items, expected] of [
    [[message('outgoing')], 'Отправлено сообщение'],
    [[message('outgoing', 'email'), message('outgoing', 'whatsapp')], 'Отправлены сообщения'],
    [[message('incoming')], 'Получено сообщение клиента'],
    [[message('incoming'), message('incoming', 'email')], 'Получены сообщения клиента'],
    [[message('incoming'), message('outgoing', 'telegram')], 'Была переписка'],
    [[message('unknown')], 'Было сообщение'],
    [[message('unknown'), message('outgoing')], 'Были сообщения'],
  ]) assert.deepEqual(communicationDayLabels(communicationDeal(items)), [expected])
  assert.deepEqual(communicationDayLabels(communicationDeal([event({ call_outcome: 'no_answer' }), message('outgoing')])), ['Была попытка связи', 'Отправлено сообщение'])
})

test('communication availability and legacy counters do not invent results', () => {
  assert.deepEqual(communicationDayLabels(communicationDeal([])), ['Сегодня коммуникаций нет'])
  assert.deepEqual(communicationDayLabels(communicationDeal([], { available: false })), ['Данные о коммуникациях недоступны'])
  assert.deepEqual(communicationDayLabels(communicationDeal([], { date: '2026-08-27', calls: 2 })), ['Данные о коммуникациях недоступны'])
  assert.deepEqual(communicationDayLabels(communicationDeal([], { calls: 1, calls_no_answer: 1, messages: 1 })), ['Был звонок · результат не определён', 'Было сообщение'])
  assert.deepEqual(communicationDayLabels(communicationDeal([], { calls: 2, messages: 3 })), ['Были звонки · результат не определён', 'Были сообщения'])
})

test('no communication is a warning badge while unavailable data is neutral, without changing deal status', () => {
  const noContact = communicationDeal([])
  const before = structuredClone(noContact)
  assert.deepEqual(reportDayLabels(noContact), [{ kind: 'no-contact', text: 'Сегодня коммуникаций нет' }])
  assert.deepEqual(noContact, before)
  assert.deepEqual(reportDayLabels(communicationDeal([], { available: false })), [{ kind: 'unavailable', text: 'Данные о коммуникациях недоступны' }])
})

test('communication labels honor frozen Moscow cutoff including legacy report metadata', () => {
  const deal = communicationDeal([
    event({ occurred_at: '2026-08-28T00:20:00+07:00', call_outcome: 'connected' }), // previous Moscow day
    event({ occurred_at: '2026-08-28T12:00:00+03:00', call_outcome: 'connected' }), // after cutoff
    event({ occurred_at: 'invalid', call_outcome: 'connected' }),
    event({ occurred_at: '2026-08-28T08:52:22Z', call_outcome: 'no_answer' }),
  ], { calls: 4 })
  assert.deepEqual(communicationDayLabels(deal), ['Была попытка связи'])
  assert.deepEqual(communicationDayLabels({ ...deal, day_scope: undefined }, cutoff), ['Была попытка связи'])
  assert.deepEqual(communicationDayLabels(deal, '2026-08-28T09:00:00+03:00'), ['Сегодня коммуникаций нет'])
})

test('task summaries expose due dates relative to snapshot, never browser today', () => {
  for (const [task, expected] of [
    [{ status: 'open', deadline: '2026-09-24T10:25:00+03:00' }, 'Задача на 24 сентября, 10:25 · не сегодня'],
    [{ status: 'open', deadline: '2026-08-28T13:00:00Z' }, 'Задача на сегодня, 16:00'],
    [{ status: 'open', deadline: '2026-08-29T01:00:00+07:00' }, 'Задача на сегодня, 21:00'],
    [{ status: 'open', overdue: true, deadline: '2026-08-28T10:00:00+03:00' }, 'Задача просрочена · срок сегодня, 10:00'],
    [{ status: 'open', overdue: true, deadline: '2026-08-27T15:00:00+03:00' }, 'Задача просрочена · срок 27 августа, 15:00'],
    [{ status: 'completed', overdue: true, completed_at: '2026-08-28T11:20:00+03:00' }, 'Задача выполнена сегодня, 11:20'],
    [{ status: 'completed' }, 'Задача выполнена · время не сохранено'],
    [{ status: 'open' }, 'Задача без срока'],
    [{ status: 'open', deadline: 'bad-date' }, 'Задача: срок не определён'],
    [{ status: 'unknown' }, 'Задача: статус не сохранён'],
  ]) assert.equal(tasksStripSummary([task], cutoff).text, expected)
  assert.match(tasksStripSummary([{ status: 'open', deadline: '2027-01-10T12:00:00+03:00' }], cutoff).text, /2027/)
  const legacy = tasksStripSummary([{ status: 'open', deadline: '2026-09-24T10:25:00+03:00' }])
  assert.match(legacy.text, /2026/)
  assert.doesNotMatch(legacy.text, /сегодня/)
  assert.equal(tasksStripSummary(undefined, cutoff).kind, 'unknown')
})

test('multiple tasks retain every row and prioritize overdue/open over completed or future tasks', () => {
  const tasks = [
    { key: 'closed', status: 'completed', deadline: '2026-08-26T10:00:00+03:00', overdue: true },
    { key: 'future', status: 'open', deadline: '2026-09-24T10:25:00+03:00' },
    { key: 'today', status: 'open', deadline: '2026-08-28T16:00:00+03:00' },
    { key: 'overdue', status: 'open', deadline: '2026-08-27T15:00:00+03:00', overdue: true },
  ]
  const before = structuredClone(tasks)
  assert.deepEqual(sortDayTasks(tasks).map((task) => task.key), ['overdue', 'today', 'future', 'closed'])
  assert.equal(tasksStripSummary(tasks, cutoff).text, 'Задач: 4 · Задача просрочена · срок 27 августа, 15:00')
  assert.equal(tasksStripSummary(tasks.slice(0, 3), cutoff).text, 'Задач: 3 · Задача на сегодня, 16:00')
  assert.deepEqual(tasks, before)
})

test('rescheduled future task retains current deadline without hiding the transfer or day obligation', () => {
  const task = { status: 'open', deadline: '2026-09-24T10:25:00+03:00', reschedules: [{ from_deadline: cutoff, to_deadline: '2026-09-24T10:25:00+03:00', occurred_at: cutoff }] }
  assert.equal(taskDeadlineLabel(task, cutoff).text, 'Задача на 24 сентября, 10:25 · не сегодня')
  assert.equal(taskStripStatus(task).kind, 'rescheduled')
  const deal = communicationDeal([])
  deal.day_scope.had_day_obligation = true
  deal.day_scope.activity_kinds = ['bitrix_task_rescheduled']
  const labels = reportDayLabels(deal).map((item) => item.text)
  assert.ok(labels.includes('Задача на этот день'))
  assert.ok(labels.includes('Задача перенесена'))
  assert.ok(labels.includes('Сегодня коммуникаций нет'))
})

// Render production components without a browser or API, as in automaticAnalysis.test.mjs.
function componentModule(file, dependencies = {}) {
  const source = ts.transpileModule(
    readFileSync(new URL(file, import.meta.url), 'utf8'),
    { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext } },
  ).outputText.replace(/from (["'])([^"']+)\1/g, (_match, _quote, specifier) => {
    const resolved = dependencies[specifier] || (specifier.startsWith('.') ? new URL(`${specifier}.ts`, import.meta.url).href : import.meta.resolve(specifier))
    return `from ${JSON.stringify(resolved)}`
  })
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
}
const { TaskDayResults } = await import(componentModule('./TaskDayResults.tsx'))

test('collapsed production task block exposes date and contains every task with transfers', () => {
  const tasks = [
    { key: 'future', subject: 'Будущая задача', status: 'open', deadline: '2026-09-24T10:25:00+03:00', reschedules: [{ from_deadline: cutoff, to_deadline: '2026-09-24T10:25:00+03:00', occurred_at: cutoff }] },
    { key: 'late', subject: 'Просроченная задача', status: 'open', overdue: true, deadline: '2026-08-27T15:00:00+03:00', reschedules: [] },
  ]
  const html = renderToStaticMarkup(createElement(TaskDayResults, { tasks, cutoffAt: cutoff }))
  assert.match(html, /<summary><span>Задач: 2 · Задача просрочена · срок 27 августа, 15:00<\/span><\/summary>/)
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/)
  assert.match(html, /Задача на 24 сентября, 10:25 · не сегодня/)
  assert.ok(html.indexOf('Просроченная задача') < html.indexOf('Будущая задача'))
  assert.match(html, /Перенесена:/)
})

test('production task block distinguishes absent legacy details from no tasks', () => {
  const legacy = renderToStaticMarkup(createElement(TaskDayResults, { cutoffAt: cutoff }))
  assert.match(legacy, /Подробности задач в этом старом срезе не сохранялись/)
  assert.doesNotMatch(legacy, /Открытых задач нет/)
  const empty = renderToStaticMarkup(createElement(TaskDayResults, { tasks: [], cutoffAt: cutoff }))
  assert.match(empty, /Открытых задач нет/)
})

const { DealReviewCard } = await import(componentModule('./DealReviewCard.tsx', {
  './CommunicationContent': componentModule('./CommunicationContent.tsx'),
}))

test('communication rows omit unknown direction without hiding channel, delivery or known directions', () => {
  const items = ['unknown', '', 'incoming', 'outgoing'].map((direction, index) => event({
    event_id: `message:${index}`, channel: 'max', direction, subject: 'Тестовое сообщение',
  }))
  const deal = {
    ...communicationDeal(items), title: 'Тестовая сделка',
    ai_context: { known: [], unknowns: [] },
    quality: { status: 'insufficient_evidence', zero_reasons: [], criteria: {
      next_action: { score: null }, value_development: { score: null }, data_collection: { score: null },
    } },
  }
  const html = renderToStaticMarkup(createElement(DealReviewCard, {
    deal, asked: [false, false], onToggleAsked() {}, onCopyScript() {}, copyNotice: '', openEventId: '', onToggleEvent() {},
  }))
  const rows = [...html.matchAll(/<span class="dc-daily-event-main">(.*?)<\/span><\/span>/g)].map((match) => match[1])
  assert.equal(rows.length, 4)
  assert.equal(rows.filter((row) => row.startsWith('<span>Max</span>')).length, 2)
  assert.equal(rows.filter((row) => row.includes('Доставлено')).length, 2)
  assert.ok(rows.some((row) => row.includes('Max · входящий')))
  assert.ok(rows.some((row) => row.includes('Max · исходящий')))
  assert.ok(rows.every((row) => !row.includes('Нет данных') && !row.includes('Max · </span>')))
  assert.match(html, /Нет данных для оценки/) // Other, meaningful missing-data notices remain.
})
