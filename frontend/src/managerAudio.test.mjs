import assert from 'node:assert/strict'
import { test } from 'node:test'
import { canRefineManagerSituation, readyManagerAudioJobId } from './managerAudio.ts'

const ready = { job_id: 'audio-1', status: 'done', attachment: { transcript: 'готово' } }

test('ready audio enables refinement without textarea text', () => {
  assert.equal(canRefineManagerSituation('', ready), true)
  assert.equal(canRefineManagerSituation('', { ...ready, status: 'running' }), false)
  assert.equal(canRefineManagerSituation('текст', null), true)
})

test('refinement retry reuses the completed transcription job', () => {
  assert.equal(readyManagerAudioJobId(ready), 'audio-1')
  assert.equal(readyManagerAudioJobId(ready), 'audio-1')
})
