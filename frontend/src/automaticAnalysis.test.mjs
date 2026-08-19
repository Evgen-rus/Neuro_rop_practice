import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  AUTOMATIC_ANALYSIS_IDLE_POLL_MS,
  AUTOMATIC_ANALYSIS_RUNNING_POLL_MS,
  automaticAnalysisCountersText,
  automaticAnalysisPollInterval,
  automaticAnalysisStageLabel,
  automaticAnalysisStatusLabel,
  shouldReloadAfterReportsPublished,
} from './automaticAnalysis.ts'

test('polls faster while the automatic packet is running', () => {
  assert.equal(automaticAnalysisPollInterval('running'), AUTOMATIC_ANALYSIS_RUNNING_POLL_MS)
  assert.equal(automaticAnalysisPollInterval('done'), AUTOMATIC_ANALYSIS_IDLE_POLL_MS)
  assert.equal(automaticAnalysisPollInterval('interrupted'), AUTOMATIC_ANALYSIS_IDLE_POLL_MS)
})

test('maps known statuses and stages without exposing backend logs', () => {
  assert.equal(automaticAnalysisStatusLabel('running'), 'Идёт автоматический анализ')
  assert.equal(automaticAnalysisStatusLabel('interrupted'), 'Автоматический пакет прерван')
  assert.equal(automaticAnalysisStageLabel('llm_analysis'), 'Анализ')
  assert.equal(automaticAnalysisStageLabel(null), null)
})

test('renders counters from the safe aggregate', () => {
  assert.equal(
    automaticAnalysisCountersText({
      processed: 27,
      total: 43,
      full: 20,
      mini: 4,
      skipped: 1,
      errors: 2,
      reports_published: 20,
    }),
    'обработано 27 из 43 · FULL 20 · MINI 4 · skip 1 · ошибок 2 · новых отчётов 20',
  )
})

test('reloads deal-control only when a new report appears', () => {
  assert.equal(shouldReloadAfterReportsPublished(0, 1), true)
  assert.equal(shouldReloadAfterReportsPublished(4, 4), false)
  assert.equal(shouldReloadAfterReportsPublished(undefined, 0), false)
})
