import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  PROMPT_LAB_LOADING_TEXT,
  applyBootstrapIfCurrent,
  applyJobIfCurrent,
  beginModuleLoad,
  labResultKind,
  visibleLabRun,
} from './promptLabWorkspace.ts'

function qhRun() {
  return {
    module_key: 'quick_help.push',
    status: 'success',
    result: { answer_contract: 'strategy_v3', client_messages: { primary: 'Текст' } },
  }
}

function followupsRun() {
  return {
    module_key: 'followups',
    status: 'success',
    result: { followup_contract: 'v1', items: [] },
  }
}

function emailRun() {
  return {
    module_key: 'full_script.email',
    status: 'success',
    result: { email_contract: 'v1', subject: 'Тема' },
  }
}

function callRun() {
  return {
    module_key: 'full_script.call',
    status: 'success',
    result: { script_contract: 'call_v1', conversation_goal: 'Цель', blocks: [] },
  }
}

function emptyState(moduleKey = 'quick_help.push') {
  return {
    moduleKey,
    loading: false,
    requestId: 1,
    currentPrompt: 'QH PROMPT',
    experimentPrompt: 'QH PROMPT',
    savedExperiment: 'QH PROMPT',
    currentRun: qhRun(),
    experimentRun: null,
    currentJob: { module_key: 'quick_help.push', job_id: 'qh-job' },
    experimentJob: null,
  }
}

test('Quick Help CURRENT → Message before bootstrap does not expose QH result as Message', () => {
  const switched = beginModuleLoad(emptyState(), 'full_script.message')
  assert.equal(switched.loading, true)
  assert.equal(switched.moduleKey, 'full_script.message')
  assert.equal(switched.currentRun, null)
  assert.equal(switched.currentPrompt, '')
  assert.equal(switched.currentJob, null)
  assert.equal(labResultKind(qhRun(), switched.moduleKey), 'empty')
  assert.equal(visibleLabRun(qhRun(), switched.moduleKey), null)
  assert.equal(labResultKind(qhRun(), 'full_script.message') !== 'full_script', true)
})

test('Quick Help → Followups does not render Quick Help result as Followups', () => {
  const switched = beginModuleLoad(emptyState(), 'followups')
  assert.equal(labResultKind(switched.currentRun, 'followups'), 'empty')
  assert.equal(labResultKind(qhRun(), 'followups'), 'empty')
  assert.notEqual(labResultKind(qhRun(), 'quick_help.push'), 'followups')
})

test('late Followups/Email bootstrap does not overwrite Call', () => {
  let state = beginModuleLoad(emptyState(), 'followups')
  const followupsId = state.requestId
  state = beginModuleLoad(state, 'full_script.email')
  const emailId = state.requestId
  state = beginModuleLoad(state, 'full_script.call')
  const callId = state.requestId

  assert.equal(applyBootstrapIfCurrent(state, followupsId, 'followups', {
    module: 'followups',
    prompt: 'FOLLOWUPS PROMPT',
    imported: followupsRun(),
  }), null)
  assert.equal(applyBootstrapIfCurrent(state, emailId, 'full_script.email', {
    module: 'full_script.email',
    prompt: 'EMAIL PROMPT',
    imported: emailRun(),
  }), null)

  const loaded = applyBootstrapIfCurrent(state, callId, 'full_script.call', {
    module: 'full_script.call',
    prompt: 'CALL PROMPT',
    imported: callRun(),
  })
  assert.ok(loaded)
  assert.equal(loaded.loading, false)
  assert.equal(loaded.moduleKey, 'full_script.call')
  assert.equal(loaded.currentPrompt, 'CALL PROMPT')
  assert.equal(loaded.currentRun?.module_key, 'full_script.call')
  assert.equal(labResultKind(loaded.currentRun, loaded.moduleKey), 'full_script')
})

test('job from previous module does not appear after switch', () => {
  let state = beginModuleLoad(emptyState(), 'full_script.message')
  state = applyBootstrapIfCurrent(state, state.requestId, 'full_script.message', {
    module: 'full_script.message',
    prompt: 'MESSAGE PROMPT',
    imported: null,
  })
  assert.ok(state)
  const ignored = applyJobIfCurrent(
    state,
    { module_key: 'quick_help.push', job_id: 'old-qh' },
    qhRun(),
    'current',
  )
  assert.equal(ignored, null)
  assert.equal(labResultKind(qhRun(), state.moduleKey), 'empty')
  assert.equal(state.currentRun, null)
})

test('after new module bootstrap, prompt and matching imported CURRENT are shown', () => {
  let state = beginModuleLoad(emptyState(), 'full_script.message')
  assert.equal(state.currentPrompt, '')
  assert.equal(PROMPT_LAB_LOADING_TEXT, 'Загружаем prompt…')
  state = applyBootstrapIfCurrent(state, state.requestId, 'full_script.message', {
    module: 'full_script.message',
    prompt: 'SYSTEM_RULES:\nmessage',
    imported: {
      module_key: 'full_script.message',
      status: 'success',
      result: { conversation_goal: 'Написать клиенту' },
    },
  })
  assert.ok(state)
  assert.equal(state.loading, false)
  assert.equal(state.currentPrompt, 'SYSTEM_RULES:\nmessage')
  assert.equal(state.experimentPrompt, 'SYSTEM_RULES:\nmessage')
  assert.equal(state.currentRun?.module_key, 'full_script.message')
  assert.equal(labResultKind(state.currentRun, 'full_script.message'), 'full_script')
  assert.equal(labResultKind(qhRun(), 'full_script.message'), 'empty')
})
