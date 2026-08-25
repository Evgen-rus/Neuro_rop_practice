import assert from 'node:assert/strict'
import { test } from 'node:test'
import { laterCheckCopy, reviewFromLabel } from './analysisReviewBanner.ts'

const NOW = new Date('2026-08-25T14:00:00+03:00')

test('reviewFromLabel uses month name without year in the current year', () => {
  assert.equal(reviewFromLabel('2026-08-20T16:39:00+03:00', NOW), 'AI-анализ от 20 августа, 16:39')
})

test('laterCheckCopy is empty after a full rewrite', () => {
  assert.equal(
    laterCheckCopy({
      createdAt: '2026-08-25T12:00:00+03:00',
      checkedAt: '2026-08-25T12:00:00+03:00',
      checkStatus: 'full',
      now: NOW,
    }),
    null,
  )
})

test('laterCheckCopy explains a same-day skip after the report', () => {
  assert.equal(
    laterCheckCopy({
      createdAt: '2026-08-20T16:39:00+03:00',
      checkedAt: '2026-08-25T12:10:00+03:00',
      checkStatus: 'skip',
      now: NOW,
    }),
    'Новых данных для обновления нет',
  )
})

test('laterCheckCopy names the day when the later check is not today', () => {
  assert.equal(
    laterCheckCopy({
      createdAt: '2026-08-20T16:39:00+03:00',
      checkedAt: '2026-08-22T18:00:00+03:00',
      checkStatus: 'mini',
      now: NOW,
    }),
    'На проверке 22 августа, 18:00 новых фактов нет',
  )
})
