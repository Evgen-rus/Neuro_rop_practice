import assert from 'node:assert/strict'
import { test } from 'node:test'
import { dealMatchesTime, ropTaskKpis } from './dealControlTimeView.ts'

const NOW = '2026-09-01T15:00:00+03:00'

function task(id, bucket, extra = {}) {
  return {
    activity_id: id,
    task_id: id,
    subject: `Задача ${id}`,
    deadline: extra.deadline || null,
    time_bucket: bucket,
    completed: extra.completion_state === 'bitrix',
    local_completed: extra.completion_state === 'local',
    completion_state: extra.completion_state || 'open',
    provider_id: 'CRM_TASKS_TASK',
    day_result: extra.day_result,
  }
}

function deal(id, bucket, extra = {}) {
  if (bucket === 'missing') return { deal_id: id, bitrix_tasks: [] }
  const item = task(id, bucket, extra)
  return { deal_id: id, primary_bitrix_task: item, bitrix_tasks: [item] }
}

function moved(fromDeadline, toDeadline, occurredAt = '2026-09-01T12:00:00+03:00') {
  return {
    was_due: true,
    reschedules: [{ from_deadline: fromDeadline, to_deadline: toDeadline, occurred_at: occurredAt }],
  }
}

test('ROP today keeps tasks that were due today or overdue and were moved forward today', () => {
  const fromToday = deal('today-moved', 'tomorrow', {
    deadline: '2026-09-02T18:00:00+03:00',
    day_result: moved('2026-09-01T18:00:00+03:00', '2026-09-02T18:00:00+03:00'),
  })
  const fromOverdue = deal('overdue-moved', 'future', {
    deadline: '2026-09-04T18:00:00+03:00',
    day_result: moved('2026-08-28T18:00:00+03:00', '2026-09-04T18:00:00+03:00'),
  })
  const stillToday = deal('today', 'today', { deadline: '2026-09-01T18:00:00+03:00' })
  const overdue = deal('overdue', 'overdue', { deadline: '2026-08-30T18:00:00+03:00' })
  const tomorrow = deal('tomorrow', 'tomorrow', { deadline: '2026-09-02T18:00:00+03:00' })
  const future = deal('future', 'future', { deadline: '2026-09-10T18:00:00+03:00' })
  const options = { keepRescheduledInToday: true, now: NOW }

  assert.equal(dealMatchesTime(fromToday, 'today', options), true)
  assert.equal(dealMatchesTime(fromToday, 'tomorrow', options), false)
  assert.equal(dealMatchesTime(fromOverdue, 'today', options), true)
  assert.equal(dealMatchesTime(fromOverdue, 'future', options), false)
  assert.equal(dealMatchesTime(stillToday, 'today', options), true)
  assert.equal(dealMatchesTime(overdue, 'today', options), true)
  assert.equal(dealMatchesTime(overdue, 'overdue', options), true)
  assert.equal(dealMatchesTime(tomorrow, 'today', options), false)
  assert.equal(dealMatchesTime(tomorrow, 'tomorrow', options), true)
  assert.equal(dealMatchesTime(future, 'today', options), false)
})

test('manager and dashboard filters still follow the current deadline', () => {
  const fromToday = deal('today-moved', 'tomorrow', {
    day_result: moved('2026-09-01T18:00:00+03:00', '2026-09-02T18:00:00+03:00'),
  })
  assert.equal(dealMatchesTime(fromToday, 'today'), false)
  assert.equal(dealMatchesTime(fromToday, 'tomorrow'), true)
  assert.equal(dealMatchesTime(fromToday, 'today', { keepRescheduledInToday: false }), false)
})

test('a future-to-future move does not stay in today', () => {
  const later = deal('later', 'future', {
    deadline: '2026-09-10T18:00:00+03:00',
    day_result: {
      was_due: false,
      reschedules: [{
        from_deadline: '2026-09-02T18:00:00+03:00',
        to_deadline: '2026-09-10T18:00:00+03:00',
        occurred_at: '2026-09-01T12:00:00+03:00',
      }],
    },
  })
  const options = { keepRescheduledInToday: true, now: NOW }
  assert.equal(dealMatchesTime(later, 'today', options), false)
  assert.equal(dealMatchesTime(later, 'future', options), true)
})

test('Unix from_deadline still counts as today or overdue', () => {
  const fromToday = deal('unix-today', 'tomorrow', {
    day_result: { was_due: false, reschedules: [{ from_deadline: '1788274800', to_deadline: '2026-09-02T18:00:00+03:00', occurred_at: NOW }] },
  })
  const fromOverdue = deal('unix-overdue', 'future', {
    day_result: { was_due: false, reschedules: [{ from_deadline: '1787929200', to_deadline: '2026-09-04T18:00:00+03:00', occurred_at: NOW }] },
  })
  const options = { keepRescheduledInToday: true, now: NOW }
  assert.equal(dealMatchesTime(fromToday, 'today', options), true)
  assert.equal(dealMatchesTime(fromOverdue, 'today', options), true)
})

test('ROP counters keep today’s plan and show reschedules separately', () => {
  const deals = [
    ...Array.from({ length: 5 }, (_, index) => deal(`today-${index}`, 'today')),
    deal('today-moved-a', 'tomorrow', { day_result: moved('2026-09-01T10:00:00+03:00', '2026-09-02T18:00:00+03:00') }),
    deal('today-moved-b', 'tomorrow', { day_result: moved('2026-09-01T11:00:00+03:00', '2026-09-02T18:00:00+03:00') }),
    deal('overdue-kept', 'overdue'),
    deal('overdue-moved', 'future', { day_result: moved('2026-08-28T18:00:00+03:00', '2026-09-04T18:00:00+03:00') }),
    deal('missing', 'missing'),
  ]
  const kpis = ropTaskKpis(deals, NOW)
  assert.equal(deals.filter((item) => dealMatchesTime(item, 'today', { keepRescheduledInToday: true, now: NOW })).length, 10)
  assert.equal(deals.filter((item) => dealMatchesTime(item, 'tomorrow', { keepRescheduledInToday: true, now: NOW })).length, 0)
  assert.equal(deals.filter((item) => dealMatchesTime(item, 'future', { keepRescheduledInToday: true, now: NOW })).length, 0)
  assert.equal(deals.filter((item) => dealMatchesTime(item, 'overdue', { keepRescheduledInToday: true, now: NOW })).length, 1)
  assert.equal(kpis.tasks_today, 7)
  assert.equal(kpis.tasks_overdue, 1)
  assert.equal(kpis.tasks_tomorrow, 0)
  assert.equal(kpis.tasks_future, 0)
  assert.equal(kpis.tasks_plan_today, 10)
  assert.equal(kpis.tasks_rescheduled_today, 3)
  assert.equal(kpis.tasks_missing, 1)
})
