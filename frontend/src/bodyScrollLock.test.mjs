import assert from 'node:assert/strict'
import { test } from 'node:test'

/**
 * Модуль держит состояние в модульных переменных и ходит в `document`,
 * поэтому для каждого теста подменяем глобальные `document`/`window` и
 * импортируем свежую копию через query-параметр: иначе счётчик держателей
 * протекал бы между тестами.
 */
async function freshModule() {
  globalThis.document = {
    body: { style: { overflow: '', paddingRight: '' } },
    documentElement: { clientWidth: 1024 },
  }
  globalThis.window = { innerWidth: 1024 }
  return import(`./bodyScrollLock.ts?case=${Math.random()}`)
}

test('single lock blocks and release restores', async () => {
  const { lockBodyScroll, bodyScrollLockHolders } = await freshModule()

  const release = lockBodyScroll()
  assert.equal(document.body.style.overflow, 'hidden')
  assert.equal(bodyScrollLockHolders(), 1)

  release()
  assert.equal(document.body.style.overflow, '')
  assert.equal(bodyScrollLockHolders(), 0)
})

test('scroll stays blocked while any layer still holds it', async () => {
  const { lockBodyScroll, bodyScrollLockHolders } = await freshModule()

  const releaseDealOverlay = lockBodyScroll()
  lockBodyScroll()
  assert.equal(bodyScrollLockHolders(), 2)

  // Оверлей сделки закрылся, но «Дожим» ещё открыт.
  releaseDealOverlay()
  assert.equal(document.body.style.overflow, 'hidden', 'скролл не должен оживать под открытым «Дожимом»')
  assert.equal(bodyScrollLockHolders(), 1)
})

/**
 * Регрессия: оверлей сделки и «Дожим» открываются одновременно и закрываются
 * не в порядке открытия. Старая схема «запомнить и вернуть `overflow`» на
 * этом сценарии возвращала `hidden` навсегда — пропадала прокрутка на всех
 * экранах, и помогала только перезагрузка.
 */
test('non-LIFO close order does not leave the page locked', async () => {
  const { lockBodyScroll, bodyScrollLockHolders } = await freshModule()

  const releaseDealOverlay = lockBodyScroll()
  const releaseAssistant = lockBodyScroll()

  // Порядок размонтирования задал не пользователь, а смена сделки.
  releaseDealOverlay()
  releaseAssistant()

  assert.equal(document.body.style.overflow, '', 'после закрытия обоих слоёв прокрутка обязана вернуться')
  assert.equal(bodyScrollLockHolders(), 0)
})

test('repeated release is ignored', async () => {
  const { lockBodyScroll, bodyScrollLockHolders } = await freshModule()

  const release = lockBodyScroll()
  release()
  release()
  release()

  assert.equal(bodyScrollLockHolders(), 0)
  assert.equal(document.body.style.overflow, '')
})

test('scrollbar compensation is applied and reverted', async () => {
  globalThis.document = {
    body: { style: { overflow: '', paddingRight: '' } },
    documentElement: { clientWidth: 1000 },
  }
  globalThis.window = { innerWidth: 1024 }
  const { lockBodyScroll } = await import(`./bodyScrollLock.ts?case=${Math.random()}`)

  const release = lockBodyScroll()
  assert.equal(document.body.style.paddingRight, '24px')

  release()
  assert.equal(document.body.style.paddingRight, '')
})
