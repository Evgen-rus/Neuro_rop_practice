import assert from 'node:assert/strict'
import { test } from 'node:test'
import { canFilterReport, dailyTaskTotals, DEFAULT_TIME_FILTER, dealMatchesTime, firstReviewDeal, matchesDailySearch, reportDayLabels, reportHeading, shouldOpenLatestReport, taskStripStatus, tasksStripSummary } from './dailyControlView.ts'

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
    const deal = { ...deals[4], day_scope: { ...deals[4].day_scope, activity_kinds: [activity] } }
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
  assert.deepEqual(reportDayLabels(deals[0]), [{ kind: 'due', text: 'Задача на этот день' }])
  assert.deepEqual(reportDayLabels(deals[1]), [{ kind: 'due', text: 'Просрочена к срезу' }])
  assert.deepEqual(reportDayLabels(deals[4]), [])
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
  assert.deepEqual(tasksStripSummary([{ status: 'open' }, { status: 'completed' }]), { kind: 'waiting', text: 'Задача ждет выполнения' })
  assert.deepEqual(tasksStripSummary([{ status: 'open', overdue: true }, { status: 'open' }]), { kind: 'overdue', text: 'Задача просрочена' })
  assert.deepEqual(tasksStripSummary([]), { kind: 'empty', text: 'Задачи в срезе не зафиксированы' })
})
