import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  DEAL_CONTROL_SKELETON_ROWS,
  dealControlSkeletonColumns,
  dealControlSkeletonMetrics,
  dealControlSkeletonNav,
  dealControlSkeletonRows,
  dealControlSkeletonStatus,
} from './dealControlSkeletonView.ts'

test('dashboard skeleton repeats the real dashboard columns and four tiles', () => {
  assert.deepEqual(
    dealControlSkeletonColumns('dashboard'),
    ['Сделка', 'Контроль', 'Этап', 'Сумма и прогноз оплаты'],
  )
  assert.deepEqual(
    dealControlSkeletonMetrics('dashboard').map(([, label]) => label),
    ['Сделок', 'Портфель', 'Задачи на сегодня', 'Просрочено'],
  )
})

test('task skeleton repeats the real task columns and six tiles', () => {
  assert.deepEqual(
    dealControlSkeletonColumns('tasks'),
    ['Сделка', 'Этап', 'Текущая задача', 'Срок', 'Выполнение'],
  )
  assert.equal(dealControlSkeletonMetrics('tasks').length, 6)
})

test('skeleton row count is stable and shapes vary between rows', () => {
  const rows = dealControlSkeletonRows()
  assert.equal(rows.length, DEAL_CONTROL_SKELETON_ROWS)
  assert.equal(dealControlSkeletonRows(3).length, 3)
  assert.equal(dealControlSkeletonRows(0).length, 0)
  assert.equal(dealControlSkeletonRows(-4).length, 0)

  const widths = rows.map((row) => row.title)
  assert.equal(new Set(widths).size, widths.length, 'одинаковые силуэты читаются как статичный блоб')
  for (const row of rows) {
    for (const value of Object.values(row)) {
      assert.ok(value > 0 && value <= 100, `ширина вне 1..100: ${value}`)
    }
  }
})

test('skeleton nav keeps only the tabs the role can open', () => {
  const admin = dealControlSkeletonNav('admin', 'dashboard').map((item) => item.label)
  assert.deepEqual(admin, [
    'Дашборд',
    'Контроль РОПа',
    'Ежедневный контроль',
    'Задачи менеджера',
    'Траектория',
    'Learning Shadow',
    'Расходы AI',
    'Команда',
  ])

  const rop = dealControlSkeletonNav('rop', 'dashboard').map((item) => item.label)
  assert.deepEqual(rop, ['Дашборд', 'Контроль РОПа', 'Ежедневный контроль', 'Задачи менеджера'])
  assert.equal(rop.includes('Расходы AI'), false)

  const manager = dealControlSkeletonNav('manager', 'manager').map((item) => item.label)
  assert.deepEqual(manager, ['Дашборд', 'Мои задачи'])
})

test('skeleton nav marks the saved view active, so the rail does not jump', () => {
  assert.deepEqual(
    dealControlSkeletonNav('rop', 'daily').filter((item) => item.active).map((item) => item.label),
    ['Ежедневный контроль'],
  )
  assert.deepEqual(
    dealControlSkeletonNav('admin', 'team').filter((item) => item.active).map((item) => item.label),
    ['Команда'],
  )
  // Менеджер сохранён на «Мои задачи» — активным должен быть именно этот пункт.
  assert.deepEqual(
    dealControlSkeletonNav('manager', 'manager').filter((item) => item.active).map((item) => item.label),
    ['Мои задачи'],
  )
})

test('status text escalates with the real wait instead of a fake percent', () => {
  assert.equal(dealControlSkeletonStatus(0), 'Загружаем контроль сделок…')
  assert.equal(dealControlSkeletonStatus(1499), 'Загружаем контроль сделок…')
  assert.match(dealControlSkeletonStatus(1500), /Собираем портфель/)
  assert.match(dealControlSkeletonStatus(7999), /Собираем портфель/)
  assert.match(dealControlSkeletonStatus(8000), /проверьте API/)
})
