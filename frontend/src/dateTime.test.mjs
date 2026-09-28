import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isoToShortDate, moscowDateInputValue, moscowMonthStart, shortDateToIso } from './dateTime.ts'

test('короткая дата разбирается в ISO в обоих направлениях', () => {
  assert.equal(shortDateToIso('28.09.2026'), '2026-09-28')
  assert.equal(shortDateToIso(' 01.01.2026 '), '2026-01-01')
  assert.equal(shortDateToIso('29.02.2024'), '2024-02-29')
  assert.equal(isoToShortDate('2026-09-28'), '28.09.2026')
  // Круговой путь не должен терять день и месяц.
  assert.equal(isoToShortDate(shortDateToIso('07.11.2026')), '07.11.2026')
})

test('несуществующий день — опечатка, а не сдвиг на март', () => {
  // Наивное `new Date('31.02.2026')` молча дало бы 3 марта, и запрос ушёл бы
  // за пределами выбранного периода без единого предупреждения.
  assert.equal(shortDateToIso('31.02.2026'), null)
  assert.equal(shortDateToIso('30.02.2026'), null)
  assert.equal(shortDateToIso('00.09.2026'), null)
  assert.equal(shortDateToIso('01.13.2026'), null)
  assert.equal(shortDateToIso('01.00.2026'), null)
})

test('неполная или лишняя дата не проходит, пока поле набирают', () => {
  // Пользователь печатает «2», «28.», «28.0» — запрос не должен прыгать.
  assert.equal(shortDateToIso('2'), null)
  assert.equal(shortDateToIso('28.'), null)
  assert.equal(shortDateToIso('28.0'), null)
  assert.equal(shortDateToIso(''), null)
  assert.equal(shortDateToIso('28.09.26'), null)
  assert.equal(shortDateToIso('28.09.2026г'), null)
  assert.equal(shortDateToIso('28/09/2026'), null)
})

test('московское сегодня остаётся корректным ISO-значением', () => {
  assert.match(moscowDateInputValue('2026-09-28T20:30:00Z'), /^2026-09-2[89]$/)
})

test('период по умолчанию идёт с первого числа текущего месяца', () => {
  assert.equal(moscowMonthStart('2026-09-28'), '2026-09-01')
  assert.equal(moscowMonthStart('2026-09-01'), '2026-09-01')
  // Январь и 31 день: месяц нельзя вычислять как «минус 30 дней», иначе
  // в начале года период тихо уезжал бы в декабрь прошлого.
  assert.equal(moscowMonthStart('2026-01-31'), '2026-01-01')
  assert.equal(moscowMonthStart('2026-12-01'), '2026-12-01')
  // Мусор на входе не должен превращаться в ложную дату первого числа.
  assert.equal(moscowMonthStart('2026-09'), '2026-09')
})
