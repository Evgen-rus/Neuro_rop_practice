import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  QUICK_HELP_REVEAL_BUDGET_MS,
  QUICK_HELP_SUMMARY_TYPE_MS,
  fragmentText,
  freshQuickHelpIdFromJob,
  latestQuickHelpEntryId,
  prefersReducedMotion,
  revealClassName,
  revealStepAt,
  shouldAnimateQuickHelpAnswer,
  summaryTypingDone,
  typedText,
} from './quickHelpReveal.ts'

test('fragmentText splits long answers into a few word groups, not letters', () => {
  const fragments = fragmentText('Клиент просит счёт и боится срока поставки завтра', 4)
  assert.ok(fragments.length <= 4)
  assert.equal(fragments.join(''), 'Клиент просит счёт и боится срока поставки завтра')
  assert.ok(fragments.every((part) => part.trim().split(/\s+/).length >= 1))
  assert.deepEqual(fragmentText('Ок'), ['Ок'])
  assert.deepEqual(fragmentText(''), [])
})

test('typedText reveals fragments in order', () => {
  const fragments = ['Понял ', 'ситуацию ', 'по сделке.']
  assert.equal(typedText(fragments, 0), '')
  assert.equal(typedText(fragments, 1), 'Понял ')
  assert.equal(typedText(fragments, 3), 'Понял ситуацию по сделке.')
  assert.equal(typedText(fragments, 9), 'Понял ситуацию по сделке.')
})

test('reveal stays inside the 1.4s budget and is sequential', () => {
  assert.equal(revealStepAt(0), 'summary')
  assert.equal(revealStepAt(QUICK_HELP_SUMMARY_TYPE_MS), 'summary')
  assert.equal(revealStepAt(749), 'summary')
  assert.equal(revealStepAt(750), 'message')
  assert.equal(revealStepAt(999), 'message')
  assert.equal(revealStepAt(1000), 'secondary')
  assert.equal(revealStepAt(1249), 'secondary')
  assert.equal(revealStepAt(1250), 'fallback')
  assert.equal(revealStepAt(1399), 'fallback')
  assert.equal(revealStepAt(QUICK_HELP_REVEAL_BUDGET_MS), 'done')
  assert.equal(summaryTypingDone(QUICK_HELP_SUMMARY_TYPE_MS - 1), false)
  assert.equal(summaryTypingDone(QUICK_HELP_SUMMARY_TYPE_MS), true)
})

test('only a fresh latest answer animates', () => {
  assert.equal(shouldAnimateQuickHelpAnswer({
    entryId: 12,
    freshEntryId: 12,
    viewingLatest: true,
    reducedMotion: false,
  }), true)
  assert.equal(shouldAnimateQuickHelpAnswer({
    entryId: 12,
    freshEntryId: 12,
    viewingLatest: false,
    reducedMotion: false,
  }), false)
  assert.equal(shouldAnimateQuickHelpAnswer({
    entryId: 11,
    freshEntryId: 12,
    viewingLatest: true,
    reducedMotion: false,
  }), false)
  assert.equal(shouldAnimateQuickHelpAnswer({
    entryId: 12,
    freshEntryId: 12,
    viewingLatest: true,
    reducedMotion: true,
  }), false)
  assert.equal(shouldAnimateQuickHelpAnswer({
    entryId: 12,
    freshEntryId: null,
    viewingLatest: true,
    reducedMotion: false,
  }), false)
  assert.equal(prefersReducedMotion({ matches: true }), true)
  assert.equal(prefersReducedMotion({ matches: false }), false)
})

test('fresh id is taken from the completed job or latest history entry', () => {
  assert.equal(freshQuickHelpIdFromJob({ quick_help_id: 31 }), 31)
  assert.equal(freshQuickHelpIdFromJob({ entry_id: 8, entry: { id: 9 } }), 8)
  assert.equal(freshQuickHelpIdFromJob({ entry: { id: 4 } }), 4)
  assert.equal(freshQuickHelpIdFromJob({}), null)
  assert.equal(latestQuickHelpEntryId([{ id: 2 }, { id: 9 }, { id: 4 }]), 9)
  assert.equal(latestQuickHelpEntryId([]), null)
  assert.equal(revealClassName('dc-manager-answer-copy', true), 'dc-manager-answer-copy dc-manager-answer-reveal')
  assert.equal(revealClassName('dc-manager-answer-copy', false), 'dc-manager-answer-copy')
})
