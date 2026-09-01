import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'
import { formatRescheduleDeadline, newestReschedules, rescheduleFromDueOnOrBeforeToday, rescheduleFromDueToday } from './taskReschedules.ts'

const changes = [
  { from_deadline: '1787903400', to_deadline: '1787907000', occurred_at: '2026-08-28T11:00:00+03:00' },
  { from_deadline: '1787907000', to_deadline: '1787923800', occurred_at: '2026-08-28T12:00:00+03:00' },
  { from_deadline: '1787923800', to_deadline: '1788163200', occurred_at: '2026-08-28T17:00:00+03:00' },
]

test('transfer deadlines format Unix seconds, milliseconds and ISO in Moscow time', () => {
  for (const value of ['1788163200', '1788163200000', '2026-08-31T08:00:00Z', '2026-08-31T11:00:00', ' 1788163200 ']) {
    assert.equal(formatRescheduleDeadline(value), '31.08.2026, 11:00')
  }
  assert.equal(formatRescheduleDeadline('1787903400'), '28.08.2026, 10:50')
  assert.equal(formatRescheduleDeadline('2027-01-01T00:00:00+03:00'), '01.01.2027, 00:00')
  assert.equal(formatRescheduleDeadline(null), 'Без срока')
  assert.equal(formatRescheduleDeadline(''), 'Без срока')
  assert.equal(formatRescheduleDeadline('bad-date'), 'Срок не определён')
})

test('old deadline on or before today is recognized from ISO and Unix values', () => {
  const now = '2026-09-01T15:00:00+03:00'
  assert.equal(rescheduleFromDueToday('2026-09-01T18:00:00+03:00', now), true)
  assert.equal(rescheduleFromDueToday('1788274800', now), true)
  assert.equal(rescheduleFromDueToday('2026-08-28T18:00:00+03:00', now), false)
  assert.equal(rescheduleFromDueOnOrBeforeToday('2026-08-28T18:00:00+03:00', now), true)
  assert.equal(rescheduleFromDueOnOrBeforeToday('2026-09-02T18:00:00+03:00', now), false)
  assert.equal(rescheduleFromDueOnOrBeforeToday('', now), false)
})

test('history is newest first without changing API data; equal timestamps keep latest entry first', () => {
  const before = structuredClone(changes)
  assert.deepEqual(newestReschedules([changes[1], changes[0], changes[2]]), [...changes].reverse())
  assert.deepEqual(changes, before)
  const sameTime = changes.map((item) => ({ ...item, occurred_at: changes[0].occurred_at }))
  assert.deepEqual(newestReschedules(sameTime), [...sameTime].reverse())
})

const source = ts.transpileModule(readFileSync(new URL('./TaskReschedulePopover.tsx', import.meta.url), 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
}).outputText.replace(/from (["'])([^"']+)\1/g, (_match, _quote, specifier) => {
  const resolved = specifier.startsWith('.') ? new URL(`${specifier}.ts`, import.meta.url).href : import.meta.resolve(specifier)
  return `from ${JSON.stringify(resolved)}`
})
const { TaskReschedulePopover } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

test('shared control renders one collapsed button, a top-layer history and readable dates', () => {
  const html = renderToStaticMarkup(createElement(TaskReschedulePopover, { task: { reschedules: changes } }))
  assert.match(html, /Перенесена · 3/)
  assert.match(html, /aria-expanded="false"/)
  assert.match(html, /popover="auto"/)
  assert.match(html, /role="dialog"/)
  assert.match(html, /Переносы срока · МСК/)
  assert.ok(html.indexOf('31.08.2026') < html.indexOf('28.08.2026, 10:50'))
  assert.doesNotMatch(html, /1787903400|1788163200|Перенесена:/)
  for (const task of [undefined, { reschedules: [] }]) {
    assert.equal(renderToStaticMarkup(createElement(TaskReschedulePopover, { task })), '')
  }
})
