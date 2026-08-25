import assert from 'node:assert/strict'
import { test } from 'node:test'
import { laterCheckCopy, reviewFromLabel, reviewHeadlineAt } from './analysisReviewBanner.ts'

const NOW = new Date('2026-08-25T14:00:00+03:00')

test('reviewFromLabel uses month name without year in the current year', () => {
  assert.equal(reviewFromLabel('2026-08-20T16:39:00+03:00', NOW), 'AI-анализ от 20 августа, 16:39')
})

test('reviewHeadlineAt keeps the report time after a full rewrite', () => {
  assert.equal(
    reviewHeadlineAt({
      createdAt: '2026-08-25T12:00:00+03:00',
      checkedAt: '2026-08-25T12:00:00+03:00',
      checkStatus: 'full',
    }),
    '2026-08-25T12:00:00+03:00',
  )
})

test('reviewHeadlineAt uses the later skip check time', () => {
  assert.equal(
    reviewHeadlineAt({
      createdAt: '2026-08-24T17:40:00+03:00',
      checkedAt: '2026-08-25T12:30:00+03:00',
      checkStatus: 'skip',
    }),
    '2026-08-25T12:30:00+03:00',
  )
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

test('laterCheckCopy names the last report after a skip', () => {
  const input = {
    createdAt: '2026-08-24T17:40:00+03:00',
    checkedAt: '2026-08-25T12:30:00+03:00',
    checkStatus: 'skip',
    now: NOW,
  }
  assert.equal(
    reviewFromLabel(reviewHeadlineAt(input), NOW),
    'AI-анализ от 25 августа, 12:30',
  )
  assert.equal(
    laterCheckCopy(input),
    'С 24 августа, 17:40 новых фактов для AI-анализа не было',
  )
})

test('laterCheckCopy uses the same wording when the later check is not today', () => {
  const input = {
    createdAt: '2026-08-20T16:39:00+03:00',
    checkedAt: '2026-08-22T18:00:00+03:00',
    checkStatus: 'mini',
    now: NOW,
  }
  assert.equal(
    reviewFromLabel(reviewHeadlineAt(input), NOW),
    'AI-анализ от 22 августа, 18:00',
  )
  assert.equal(
    laterCheckCopy(input),
    'С 20 августа, 16:39 новых фактов для AI-анализа не было',
  )
})
