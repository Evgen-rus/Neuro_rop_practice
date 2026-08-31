import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'


function moduleUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
}


function transpiledModule(file, dependencies = {}) {
  const source = ts.transpileModule(
    readFileSync(new URL(file, import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.ESNext } },
  ).outputText.replace(/from (["'])([^"']+)\1/g, (_match, _quote, specifier) => {
    const resolved = dependencies[specifier] || new URL(`${specifier}.ts`, import.meta.url).href
    return `from ${JSON.stringify(resolved)}`
  })
  return moduleUrl(source)
}


const apiStub = moduleUrl(`
export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status }
}
export async function fetchDealCallTranscript() { throw new Error('unexpected default call') }
export async function fetchDealCommunicationThread() { throw new Error('unexpected default call') }
`)
const dialogModule = await import(transpiledModule('./communicationDialog.ts', { './api': apiStub }))
const {
  communicationDialogReducer,
  getCachedCommunication,
  initialCommunicationDialogState,
  loadCommunicationPayload,
  storeCommunicationPayload,
} = dialogModule

const target = { dealId: '7', eventId: 'event-1', channel: 'whatsapp' }


test('first open immediately enters loading state', () => {
  const state = communicationDialogReducer(initialCommunicationDialogState, {
    type: 'OPEN',
    target,
    cached: null,
  })
  assert.equal(state.open, true)
  assert.equal(state.phase, 'loading')
  assert.equal(state.payload, null)
})


test('text target immediately invokes thread loader and returns thread payload', async () => {
  let calls = 0
  const thread = {
    deal_id: '7',
    anchor_event_id: 'event-1',
    conversation_key: 'conversation:v1:test',
    conversation_scope: 'contact',
    date: '2026-08-31',
    timezone: 'Europe/Moscow',
    channel: 'whatsapp',
    messages: [{ event_id: 'event-1', text: 'Да' }],
    truncated: false,
  }
  const payload = await loadCommunicationPayload(target, {
    transcript: async () => { throw new Error('transcript should not load') },
    thread: async (dealId, eventId) => {
      calls += 1
      assert.equal(dealId, '7')
      assert.equal(eventId, 'event-1')
      return thread
    },
  })
  assert.equal(calls, 1)
  assert.equal(payload.kind, 'thread')
  assert.equal(payload.value.messages[0].text, 'Да')
})


test('call target uses transcript loader instead of thread loader', async () => {
  const payload = await loadCommunicationPayload(
    { ...target, channel: 'call' },
    {
      transcript: async () => ({ deal_id: '7', event_id: 'event-1', text: 'Разговор', truncated: false }),
      thread: async () => { throw new Error('thread should not load') },
    },
  )
  assert.equal(payload.kind, 'transcript')
  assert.equal(payload.value.text, 'Разговор')
})


test('thread cache is shared by conversation day across message anchors', () => {
  const payload = {
    kind: 'thread',
    value: {
      deal_id: '7',
      anchor_event_id: 'event-1',
      conversation_key: 'conversation:v1:shared',
      date: '2026-08-31',
      timezone: 'Europe/Moscow',
      channel: 'whatsapp',
      messages: [
        { event_id: 'event-1', channel: 'whatsapp', text: 'Да' },
        { event_id: 'event-2', channel: 'whatsapp', text: 'После 15' },
      ],
      truncated: false,
    },
  }
  storeCommunicationPayload(target, payload)
  assert.equal(
    getCachedCommunication({ ...target, eventId: 'event-2' }),
    payload,
  )
})
