import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  STRATEGY_FALLBACK_LABELS,
  answerModeClassName,
  currentEntryForMode,
  entriesForMode,
  entryMode,
  isAutoOrigin,
  missingCurrentModes,
  pressureLever,
  strategyLabel,
  visibleLifehack,
  workspaceModeClassName,
} from './dealPush.ts'

const v2 = {
  id: 1,
  deal_id: '101',
  source_report_id: 17,
  situation_review_id: 21,
  question: 'подскажи что делать',
  origin: 'manager',
  content: {
    answer_contract: 'strategy_v2',
    situation_summary: 'Клиент молчит',
    next_action: 'Написать короткий вопрос',
    expected_result: 'Получить ответ',
    crm_checklist: [],
    client_messages: { primary: 'A', alternative: 'B', pattern_break: 'C' },
    lifehacks: [],
    fallback_action: 'Сменить канал',
  },
  created_at: '2026-08-13T10:00:00+03:00',
}

const push = {
  id: 3,
  deal_id: '101',
  source_report_id: 17,
  situation_review_id: 21,
  question: 'Сформируй текущий дожим сделки',
  mode: 'push',
  origin: 'auto',
  content: {
    answer_contract: 'strategy_v3',
    mode: 'push',
    situation_summary: 'Нужно закрыть согласование',
    next_action: 'Дать экспертный следующий шаг',
    expected_result: 'Получить решение',
    crm_checklist: [],
    pressure_lever: {
      title: 'Отстройка через надёжность',
      rationale: 'Клиент сравнивает узлы, а не цену.',
    },
    strategy_labels: {
      primary: 'Через надёжность',
      alternative: 'Через сроки',
      pattern_break: 'Через согласование',
    },
    client_messages: { primary: 'A', alternative: 'B', pattern_break: 'C' },
    lifehacks: [{ tactic_id: 'MT-1', title: 'Смена канала', action: 'Написать', why_relevant: 'Молчит', conditions: 'Есть мессенджер' }],
    fallback_action: 'Сменить канал',
  },
  created_at: '2026-08-13T11:00:00+03:00',
}

test('legacy entries stay readable as reanimator without labels or lever', () => {
  assert.equal(entryMode(v2), 'reanimator')
  assert.equal(isAutoOrigin(v2), false)
  assert.equal(strategyLabel(v2.content, 'primary'), STRATEGY_FALLBACK_LABELS.primary)
  assert.equal(pressureLever(v2.content), null)
})

test('strategy tabs use semantic labels instead of 1/2/3', () => {
  assert.equal(strategyLabel(push.content, 'primary'), 'Через надёжность')
  assert.equal(strategyLabel(push.content, 'alternative'), 'Через сроки')
  assert.equal(strategyLabel(push.content, 'pattern_break'), 'Через согласование')
})

test('current recommendation is per mode and does not mix voices', () => {
  const reanimator = { ...v2, id: 2, mode: 'reanimator', origin: 'auto' }
  const laterPush = { ...push, id: 4 }
  const entries = [laterPush, push, reanimator, v2]
  assert.equal(currentEntryForMode(entries, 'push', 17, 21)?.id, 4)
  assert.equal(currentEntryForMode(entries, 'reanimator', 17, 21)?.id, 2)
  assert.deepEqual(entriesForMode(entries, 'push').map((item) => item.id), [3, 4])
  assert.deepEqual(missingCurrentModes({ push, reanimator: null }), ['reanimator'])
  assert.deepEqual(missingCurrentModes({ push, reanimator }), [])
})

test('lifehacks are shown one at a time with position', () => {
  const lifehacks = push.content.lifehacks
  const extra = [...lifehacks, { ...lifehacks[0], tactic_id: 'MT-2', title: 'Второй' }]
  assert.equal(visibleLifehack([], 0), null)
  assert.equal(visibleLifehack(extra, 0)?.index, 0)
  assert.equal(visibleLifehack(extra, 0)?.total, 2)
  assert.equal(visibleLifehack(extra, 1)?.item.title, 'Второй')
  assert.equal(visibleLifehack(extra, 9)?.index, 1)
})

test('mode class names keep push warm and reanimator cool', () => {
  assert.equal(answerModeClassName('push'), 'dc-manager-answer mode-push')
  assert.equal(answerModeClassName('reanimator'), 'dc-manager-answer mode-reanimator')
  assert.equal(workspaceModeClassName('push'), 'dc-manager-assistant-modal mode-push')
  assert.equal(workspaceModeClassName('reanimator'), 'dc-manager-assistant-modal mode-reanimator')
  assert.ok(pressureLever(push.content)?.title.includes('надёжность'))
})
