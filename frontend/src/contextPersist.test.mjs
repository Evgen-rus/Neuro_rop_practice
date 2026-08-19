import assert from 'node:assert/strict'
import { test } from 'node:test'
import { copyTextToClipboard, persistTextAndOpenUrl } from './contextPersist.ts'

test('copyTextToClipboard writes exact text', async () => {
  const written = []
  const copied = await copyTextToClipboard('нужный контекст', {
    writeText: async (value) => { written.push(value) },
  })
  assert.equal(copied, true)
  assert.deepEqual(written, ['нужный контекст'])
})

test('copyTextToClipboard fails when clipboard is missing', async () => {
  assert.equal(await copyTextToClipboard('текст', undefined), false)
  assert.equal(await copyTextToClipboard('', { writeText: async () => undefined }), false)
})

test('copyTextToClipboard fails when writeText throws', async () => {
  const copied = await copyTextToClipboard('текст', {
    writeText: async () => { throw new Error('denied') },
  })
  assert.equal(copied, false)
})

test('persistTextAndOpenUrl copies and opens on happy path', async () => {
  const copiedValues = []
  const opened = []
  const result = await persistTextAndOpenUrl('контекст менеджера', 'https://example.test/crm/deal/details/42/', {
    copy: async (value) => { copiedValues.push(value); return true },
    open: (url) => { opened.push(url); return true },
  })
  assert.deepEqual(result, { copied: true, opened: true })
  assert.deepEqual(copiedValues, ['контекст менеджера'])
  assert.deepEqual(opened, ['https://example.test/crm/deal/details/42/'])
})

test('persistTextAndOpenUrl still opens Bitrix when clipboard fails', async () => {
  const opened = []
  const result = await persistTextAndOpenUrl('контекст', 'https://example.test/deal/7/', {
    copy: async () => false,
    open: (url) => { opened.push(url); return true },
  })
  assert.deepEqual(result, { copied: false, opened: true })
  assert.deepEqual(opened, ['https://example.test/deal/7/'])
})

test('persistTextAndOpenUrl still opens Bitrix when copy throws', async () => {
  const opened = []
  const result = await persistTextAndOpenUrl('контекст', 'https://example.test/deal/9/', {
    copy: async () => { throw new Error('clipboard exploded') },
    open: (url) => { opened.push(url); return true },
  })
  assert.deepEqual(result, { copied: false, opened: true })
  assert.deepEqual(opened, ['https://example.test/deal/9/'])
})

test('persistTextAndOpenUrl still copies when opening the URL throws', async () => {
  const copiedValues = []
  const result = await persistTextAndOpenUrl('контекст', 'https://example.test/deal/3/', {
    copy: async (value) => { copiedValues.push(value); return true },
    open: () => { throw new Error('popup blocked') },
  })
  assert.deepEqual(result, { copied: true, opened: false })
  assert.deepEqual(copiedValues, ['контекст'])
})

test('persistTextAndOpenUrl opens before waiting for copy', async () => {
  const order = []
  let releaseCopy
  const copyStarted = new Promise((resolve) => { releaseCopy = resolve })
  const pending = persistTextAndOpenUrl('текст', 'https://example.test/deal/1/', {
    copy: async () => {
      order.push('copy-start')
      await copyStarted
      order.push('copy-end')
      return true
    },
    open: () => {
      order.push('open')
      return true
    },
  })
  assert.deepEqual(order, ['open', 'copy-start'])
  releaseCopy()
  assert.deepEqual(await pending, { copied: true, opened: true })
  assert.deepEqual(order, ['open', 'copy-start', 'copy-end'])
})
