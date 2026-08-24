import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  WINDOW_EVENT_CATEGORIES,
  allWindowCategoriesSelected,
  buildWindowExport,
  defaultWindowCategories,
  filenameFromContentDisposition,
  filterWindowEvents,
  toggleAllWindowCategories,
  toggleWindowCategory,
  windowExportFilename,
  windowVisibleSummary,
} from './trajectoryWindow.ts'

const events = [
  { event_id: 'd-call', entity_type: 'deal', entity_id: '101', category: 'communications', label: 'Звонок' },
  { event_id: 'd-task', entity_type: 'deal', entity_id: '101', category: 'tasks', label: 'Задача' },
  { event_id: 'l-mail', entity_type: 'lead', entity_id: '202', category: 'communications', label: 'Письмо' },
  { event_id: 'neuro', entity_type: 'deal', entity_id: '101', category: 'neurorop', label: 'Рекомендация просмотрена' },
]

test('page category=all opens the drawer with every category selected', () => {
  assert.deepEqual(defaultWindowCategories('all'), [...WINDOW_EVENT_CATEGORIES])
  assert.equal(allWindowCategoriesSelected(defaultWindowCategories('all')), true)
})

test('page category seeds drawer checkboxes but keeps other events available', () => {
  const selected = defaultWindowCategories('communications')
  assert.deepEqual(selected, ['communications'])
  const visible = filterWindowEvents(events, selected)
  assert.deepEqual(visible.map((item) => item.event_id), ['d-call', 'l-mail'])
  assert.deepEqual(
    filterWindowEvents(events, toggleWindowCategory(selected, 'neurorop')).map((item) => item.event_id),
    ['d-call', 'l-mail', 'neuro'],
  )
})

test('local multi-category filter uses deals by entity_type and other lanes by category', () => {
  const visible = filterWindowEvents(events, ['deals', 'communications', 'neurorop'])
  assert.deepEqual(visible.map((item) => item.event_id), ['d-call', 'd-task', 'l-mail', 'neuro'])
  const summary = windowVisibleSummary(visible)
  assert.equal(summary.events, 4)
  assert.equal(summary.entities, 2)
})

test('summary recounts only currently visible events and unique entities', () => {
  const visible = filterWindowEvents(events, ['tasks'])
  assert.deepEqual(windowVisibleSummary(visible), { events: 1, entities: 1 })
  assert.deepEqual(windowVisibleSummary(filterWindowEvents(events, [])), { events: 0, entities: 0 })
})

test('hourly JSON contains only locally selected events', () => {
  const selected = ['deals', 'communications', 'neurorop']
  const visible = filterWindowEvents(events, selected)
  const payload = buildWindowExport({
    manager_id: '10',
    manager_name: 'Пахомов Александр',
    period: { from: '2026-08-24T10:00:00+03:00', to: '2026-08-24T11:00:00+03:00' },
    categories: selected,
    q: 'Альфа',
    events: visible,
  })
  assert.equal(payload.manager_id, '10')
  assert.deepEqual(payload.categories, selected)
  assert.equal(payload.q, 'Альфа')
  assert.equal(payload.event_count, 4)
  assert.equal(payload.entities, 2)
  assert.deepEqual(payload.events.map((item) => item.event_id), ['d-call', 'd-task', 'l-mail', 'neuro'])
  assert.equal(
    windowExportFilename('10', payload.period.from, payload.period.to),
    'trajectory-manager-10-2026-08-24-10-00_11-00.json',
  )
})

test('Все события selects and clears every category without a tri-state', () => {
  const all = toggleAllWindowCategories([])
  assert.equal(allWindowCategoriesSelected(all), true)
  assert.deepEqual(toggleAllWindowCategories(all), [])
})

test('filenameFromContentDisposition reads a regular attachment name', () => {
  assert.equal(
    filenameFromContentDisposition(
      'attachment; filename="trajectory-2026-08-24-all-managers.json"',
      'fallback.json',
    ),
    'trajectory-2026-08-24-all-managers.json',
  )
})
