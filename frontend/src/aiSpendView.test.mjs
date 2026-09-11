import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  buildAiSpendEventsQuery,
  buildAiSpendPeriodQuery,
  formatSpendDelta,
  spendChartIndexFromSvgX,
  spendChartSeries,
  spendChartValue,
  spendDayTitle,
  spendShiftIsoDate,
} from './aiSpendView.ts'

test('period query keeps 7/30 without dates and adds custom range', () => {
  assert.equal(buildAiSpendPeriodQuery('7', '2026-08-01', '2026-09-09').toString(), 'preset=7')
  assert.equal(
    buildAiSpendPeriodQuery('custom', '2026-08-11', '2026-09-09').toString(),
    'preset=custom&from=2026-08-11&to=2026-09-09',
  )
})

test('events query adds search and attention without empty filters', () => {
  const query = buildAiSpendEventsQuery({
    preset: '30',
    fromDate: '',
    toDate: '',
    q: '19021',
    attention: 'errors',
    page: 2,
  })
  assert.equal(query.get('q'), '19021')
  assert.equal(query.get('attention'), 'errors')
  assert.equal(query.get('page'), '2')
  assert.equal(query.get('kind_group'), null)
})

test('delta formatting stays honest and signed', () => {
  assert.equal(formatSpendDelta(null), null)
  assert.deepEqual(formatSpendDelta(0), { text: '0%', tone: 'flat' })
  assert.deepEqual(formatSpendDelta(-12), { text: '↓ 12%', tone: 'down' })
  assert.deepEqual(formatSpendDelta(8.4), { text: '↑ 8,4%', tone: 'up' })
})

test('chart series uses kind-group daily data when filtered', () => {
  const analytics = {
    daily_series: [
      { date: '2026-09-09', estimated_cost_rub: 10, paid_calls: 2, total_tokens: 100, unknown_cost_calls: 0 },
    ],
    kind_groups: [
      {
        id: 'quick_help',
        daily: [
          { date: '2026-09-09', estimated_cost_rub: 3, paid_calls: 1, total_tokens: 40, unknown_cost_calls: 0 },
        ],
      },
    ],
  }
  const series = spendChartSeries(analytics, 'quick_help')
  assert.equal(spendChartValue(series[0], 'cost'), 3)
  assert.equal(spendChartValue(series[0], 'calls'), 1)
})

test('chart hover index follows the plotted line, not the empty side padding', () => {
  assert.equal(spendChartIndexFromSvgX(10, 5), 0)
  assert.equal(spendChartIndexFromSvgX(790, 5), 4)
  assert.equal(spendChartIndexFromSvgX(400, 5), 2)
})

test('day titles mark today yesterday and the day before', () => {
  assert.equal(spendShiftIsoDate('2026-09-10', -1), '2026-09-09')
  assert.equal(spendDayTitle('2026-09-10', '2026-09-10', '10 сентября'), 'Сегодня · 10 сентября')
  assert.equal(spendDayTitle('2026-09-09', '2026-09-10', '09 сентября'), 'Вчера · 09 сентября')
  assert.equal(spendDayTitle('2026-09-08', '2026-09-10', '08 сентября'), 'Позавчера · 08 сентября')
  assert.equal(spendDayTitle('2026-08-01', '2026-09-10', '01 августа'), '01 августа')
})
