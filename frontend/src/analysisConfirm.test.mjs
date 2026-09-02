import assert from 'node:assert/strict'
import { test } from 'node:test'
import { analysisConfirmCopy, canForceFullAnalysis, shouldConfirmAnalysis } from './analysisConfirm.ts'

test('only admin can choose forced full analysis', () => {
  assert.equal(canForceFullAnalysis('admin'), true)
  assert.equal(canForceFullAnalysis('rop'), false)
  assert.equal(canForceFullAnalysis('manager'), false)
})

test('admin always confirms, others only when a report already exists', () => {
  assert.equal(shouldConfirmAnalysis('admin', false), true)
  assert.equal(shouldConfirmAnalysis('admin', true), true)
  assert.equal(shouldConfirmAnalysis('rop', true), true)
  assert.equal(shouldConfirmAnalysis('manager', false), false)
})

test('non-admin copy keeps the current check-only window', () => {
  const copy = analysisConfirmCopy({ role: 'rop', hasReport: true })
  assert.equal(copy.title, 'Проверить новые данные?')
  assert.equal(copy.checkLabel, 'Проверить и обновить')
  assert.equal(copy.fullLabel, null)
})

test('admin with a report can choose check or paid full rewrite', () => {
  const copy = analysisConfirmCopy({ role: 'admin', hasReport: true })
  assert.equal(copy.title, 'Как обновить анализ?')
  assert.equal(copy.checkLabel, 'Проверить как сейчас')
  assert.equal(copy.fullLabel, 'Полный анализ')
  assert.match(copy.note, /платный/)
})

test('admin without a report also gets the same two choices', () => {
  const copy = analysisConfirmCopy({ role: 'admin', hasReport: false })
  assert.equal(copy.title, 'Как провести анализ?')
  assert.equal(copy.checkLabel, 'Проверить и запустить')
  assert.equal(copy.fullLabel, 'Полный анализ')
})
