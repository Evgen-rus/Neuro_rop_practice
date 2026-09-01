import assert from 'node:assert/strict'
import { test } from 'node:test'
import { accountInTeamScope, appendUniqueIds, canDeactivateAccount, createAccountError, createAccountNotice, roleLabel } from './teamAdminView.ts'

test('appendUniqueIds keeps current Bitrix IDs and skips duplicates', () => {
  assert.deepEqual(appendUniqueIds(['1421', '2653'], '2775'), ['1421', '2653', '2775'])
  assert.deepEqual(appendUniqueIds(['1421', '2653'], ' 1421 '), ['1421', '2653'])
  assert.deepEqual(appendUniqueIds(['1421'], '  '), ['1421'])
})

test('accountInTeamScope matches only the Bitrix manager_id', () => {
  const manager = { id: 6, login: 'ahramovich', role: 'manager', manager_id: '2775', is_active: true }
  assert.equal(accountInTeamScope(manager, ['1421', '2775']), true)
  assert.equal(accountInTeamScope(manager, ['1421', '2653']), false)
  assert.equal(accountInTeamScope({ ...manager, manager_id: null }, ['2775']), false)
})

test('canDeactivateAccount never turns off the current session', () => {
  const admin = { id: 1, login: 'madboss', role: 'admin', manager_id: null, is_active: true }
  const manager = { id: 6, login: 'ahramovich', role: 'manager', manager_id: '2775', is_active: true }
  assert.equal(canDeactivateAccount(admin, 1), false)
  assert.equal(canDeactivateAccount(manager, 1), true)
  assert.equal(canDeactivateAccount({ ...manager, is_active: false }, 1), false)
  assert.equal(roleLabel('manager'), 'Менеджер')
  assert.equal(roleLabel('admin'), 'Админ')
})

test('createAccountError asks manager for Bitrix ID and lets rop skip it', () => {
  const base = { login: 'ivanov', password: 'secret', confirmPassword: 'secret', managerId: '' }
  assert.equal(createAccountError({ ...base, role: 'manager' }), 'Менеджеру нужен Bitrix ID ответственного.')
  assert.equal(createAccountError({ ...base, role: 'rop' }), null)
  assert.equal(createAccountError({ ...base, role: 'rop', confirmPassword: 'other' }), 'Пароли не совпадают.')
  assert.equal(createAccountNotice('rop'), 'РОП создан. Он видит сделки выборки команды, Bitrix ID не нужен.')
})
