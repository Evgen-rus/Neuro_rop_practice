import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  defaultDealControlView,
  initialDealControlFilters,
  resolveDealControlView,
  viewsAllowedForRole,
} from './dealControlStartView.ts'

test('manager starts on own tasks, other roles keep current landing', () => {
  assert.equal(defaultDealControlView('manager'), 'manager')
  assert.equal(defaultDealControlView('rop'), 'rop')
  assert.equal(defaultDealControlView('admin'), 'dashboard')
})

test('refresh restores last tab when the role can open it', () => {
  assert.equal(resolveDealControlView('manager', 'dashboard'), 'dashboard')
  assert.equal(resolveDealControlView('manager', 'manager'), 'manager')
  assert.equal(resolveDealControlView('rop', 'daily'), 'daily')
  assert.equal(resolveDealControlView('admin', 'team'), 'team')
})

test('refresh ignores a tab the role cannot open and falls back to role start', () => {
  assert.equal(resolveDealControlView('manager', 'rop'), 'manager')
  assert.equal(resolveDealControlView('manager', 'team'), 'manager')
  assert.equal(resolveDealControlView('rop', 'team'), 'rop')
  assert.equal(resolveDealControlView('rop', 'not-a-view'), 'rop')
  assert.equal(resolveDealControlView('admin', null), 'dashboard')
})

test('manager landing uses today and own manager filter like clicking the tab', () => {
  assert.deepEqual(
    initialDealControlFilters('manager', 'manager', '2775'),
    { managerFilter: '2775', timeView: 'today' },
  )
  assert.deepEqual(
    initialDealControlFilters('manager', 'dashboard', '2775'),
    { managerFilter: '', timeView: 'all' },
  )
  assert.deepEqual(
    initialDealControlFilters('admin', 'manager', '2775'),
    { managerFilter: '', timeView: 'today' },
  )
})

test('manager cannot open admin or ROP-only tabs', () => {
  assert.deepEqual(viewsAllowedForRole('manager'), ['dashboard', 'manager'])
})
