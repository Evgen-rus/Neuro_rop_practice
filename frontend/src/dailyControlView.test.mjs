import assert from 'node:assert/strict'
import { test } from 'node:test'
import { canFilterReport, DEFAULT_TIME_FILTER, dealMatchesTime, firstReviewDeal, reportDayLabels, shouldOpenLatestReport } from './dailyControlView.ts'

const deals = ['today', 'overdue', 'missing', 'tomorrow', 'future', 'unscheduled'].map((bucket, index) => ({
  deal_id: String(index), manager_id: '1', status: index === 3 ? 'red' : 'yellow', bitrix_task_time_bucket: bucket,
  day_scope: { business_date: '2026-08-27', cutoff_at: '2026-08-27T15:45:00+03:00', task_buckets: bucket === 'missing' ? [] : [bucket], activity_kinds: [], legacy: false },
}))

test('default day keeps due and overdue tasks, not an idle deal with no task', () => {
  const filtered = deals.filter((deal) => dealMatchesTime(deal, DEFAULT_TIME_FILTER))
  assert.deepEqual(filtered.map((deal) => deal.bitrix_task_time_bucket), ['today', 'overdue'])
  assert.equal(firstReviewDeal(filtered, '1').deal_id, '0')
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

test('future tasks with work belong to the report day and the left row explains the work', () => {
  for (const activity of ['call', 'message', 'stage_change', 'comment', 'bitrix_task_completed', 'local_task_completed', 'checklist_completed']) {
    const deal = { ...deals[4], day_scope: { ...deals[4].day_scope, activity_kinds: [activity] } }
    assert.equal(dealMatchesTime(deal, 'today'), true, activity)
    assert.equal(dealMatchesTime(deal, 'future'), true)
    assert.equal(reportDayLabels(deal).filter((item) => item.kind === 'work').length, 1)
  }
  const completed = { ...deals[2], day_scope: { ...deals[2].day_scope, activity_kinds: ['bitrix_task_completed'] } }
  assert.equal(dealMatchesTime(completed, 'today'), true)
  assert.deepEqual(reportDayLabels(deals[0]), [{ kind: 'due', text: 'Задача на этот день' }])
  assert.deepEqual(reportDayLabels(deals[1]), [{ kind: 'due', text: 'Просрочена к срезу' }])
  assert.deepEqual(reportDayLabels(deals[4]), [])
})

test('a completed primary task cannot hide a different open task due on the report day', () => {
  const deal = { ...deals[2], day_scope: { ...deals[2].day_scope, task_buckets: ['today', 'future'] } }
  assert.equal(dealMatchesTime(deal, 'today'), true)
  assert.equal(dealMatchesTime(deal, 'future'), true)
})

test('background refresh opens a new report only outside an active review or chosen history', () => {
  assert.equal(shouldOpenLatestReport(10, 11, false, false), true)
  assert.equal(shouldOpenLatestReport(undefined, 11, false, false), true)
  assert.equal(shouldOpenLatestReport(10, 11, true, false), false)
  assert.equal(shouldOpenLatestReport(10, 11, false, true), false)
  assert.equal(shouldOpenLatestReport(10, 10, false, false), false)
  assert.equal(shouldOpenLatestReport(10, null, false, false), false)
})
