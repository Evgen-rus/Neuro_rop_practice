import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'
import {
  AUTOMATIC_ANALYSIS_IDLE_POLL_MS,
  AUTOMATIC_ANALYSIS_RUNNING_POLL_MS,
  automaticAnalysisCountersText,
  automaticAnalysisCurrentText,
  automaticAnalysisPollInterval,
  automaticAnalysisStageLabel,
  automaticAnalysisStatusLabel,
  canViewAutomaticAnalysis,
  shouldReloadAfterAutomaticAnalysis,
  shouldReloadAfterReportsPublished,
} from './automaticAnalysis.ts'

test('only admin can view the automatic packet', () => {
  for (const role of ['admin', 'rop', 'manager', '']) {
    assert.equal(canViewAutomaticAnalysis(role), role === 'admin')
  }
})

// Compile the actual TSX component for a DOM-free render test, without a test server.
const panelSource = ts.transpileModule(
  readFileSync(new URL('./AutomaticAnalysisPanel.tsx', import.meta.url), 'utf8'),
  { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext } },
).outputText.replace(/from (["'])([^"']+)\1/g, (_match, _quote, specifier) => {
  const resolved = specifier.startsWith('.')
    ? new URL(`${specifier}.ts`, import.meta.url).href
    : import.meta.resolve(specifier)
  return `from ${JSON.stringify(resolved)}`
})
const { AutomaticAnalysisPanel } = await import(`data:text/javascript;base64,${Buffer.from(panelSource).toString('base64')}`)
const packet = {
  status: 'done', business_date: '2026-08-28', processed: 3, total: 3,
  full: 1, mini: 1, skipped: 1, errors: 0, reports_published: 1,
  started_at: '2026-08-28T10:00:00+03:00', updated_at: '2026-08-28T10:15:00+03:00',
  details: [
    { deal_id: '101', title: 'Тестовая сделка', decision: 'full', incremental: true, reasons: ['Изменилась расшифровка разговора'] },
    { deal_id: '202', title: 'Тестовая MINI', decision: 'mini', incremental: false, reasons: ['Просрочена открытая задача'] },
  ],
}

test('actual panel is collapsed initially and contains full/mini details for admin', () => {
  const html = renderToStaticMarkup(createElement(AutomaticAnalysisPanel, { snapshot: packet, role: 'admin' }))
  assert.match(html, /^<details[^>]*><summary>/)
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/)
  assert.match(html, /обработано 3 из 3/)
  assert.doesNotMatch(html, /2026-08-28 · /)
  assert.doesNotMatch(html, /Подробности FULL \/ MINI/)
  assert.match(html, /FULL \(инкрементальный LLM-анализ\) · #101 · Тестовая сделка/)
  assert.match(html, /MINI · #202 · Тестовая MINI/)
  assert.match(html, /Просрочена открытая задача/)
  assert.match(html, /обновлено 28\.08, 10:15/)
})

test('actual panel renders nothing for rop and manager, even if details were supplied', () => {
  for (const role of ['rop', 'manager', '']) {
    assert.equal(renderToStaticMarkup(createElement(AutomaticAnalysisPanel, { snapshot: packet, role })), '')
  }
  assert.equal(renderToStaticMarkup(createElement(AutomaticAnalysisPanel, { snapshot: null, role: 'admin' })), '')
})

test('empty and running packets do not claim that full/mini results exist', () => {
  for (const status of ['done', 'running']) {
    const html = renderToStaticMarkup(createElement(AutomaticAnalysisPanel, {
      snapshot: { ...packet, status, details: [] }, role: 'admin',
    }))
    assert.match(html, status === 'running'
      ? /В этом пакете пока нет результатов FULL \/ MINI/
      : /В этом пакете нет результатов FULL \/ MINI/)
  }
})

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

test('shows a short current-deal line only while the packet is running', () => {
  assert.equal(
    automaticAnalysisCurrentText({
      status: 'running',
      current: { title: 'ООО Ромашка', stage: 'llm_analysis' },
      current_stage: 'llm_analysis',
    }),
    'сейчас: ООО Ромашка · Анализ',
  )
  assert.equal(
    automaticAnalysisCurrentText({
      status: 'done',
      current: { title: 'ООО Ромашка', stage: 'done' },
      current_stage: 'done',
    }),
    null,
  )
  assert.equal(
    automaticAnalysisCurrentText({
      status: 'running',
      current: null,
      current_stage: 'llm_analysis',
    }),
    null,
  )
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

test('reloads deal-control when an automatic packet finishes without a full report', () => {
  assert.equal(shouldReloadAfterAutomaticAnalysis(
    { status: 'running', started_at: '2026-08-20T08:30:00+03:00', reports_published: 0 },
    { status: 'done', started_at: '2026-08-20T08:30:00+03:00', reports_published: 0 },
  ), true)
})

test('reloads deal-control when polling first sees a different completed packet', () => {
  assert.equal(shouldReloadAfterAutomaticAnalysis(
    { status: 'done', started_at: '2026-08-20T08:00:00+03:00', reports_published: 2 },
    { status: 'done', started_at: '2026-08-20T08:30:00+03:00', reports_published: 0 },
  ), true)
})

test('does not reload deal-control repeatedly for the same completed packet', () => {
  const snapshot = { status: 'done', started_at: '2026-08-20T08:30:00+03:00', reports_published: 0 }
  assert.equal(shouldReloadAfterAutomaticAnalysis(snapshot, snapshot), false)
})
