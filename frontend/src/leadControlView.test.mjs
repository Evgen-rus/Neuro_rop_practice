import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  MAX_LEAD_CONTROL_SELECTION,
  buildLeadControlSearchRequest,
  toggleLeadSelection,
} from './leadControlView.ts'

test('lead control requests the server-owned 30-day stage group and optional manager filter', () => {
  assert.deepEqual(buildLeadControlSearchRequest('active', '42'), {
    entity_type: 'lead',
    created_days: 30,
    modified_days: 0,
    limit: 100,
    priority: null,
    pipeline_ids: [],
    stage_ids: [],
    lead_control_group: 'active',
    manager_id: '42',
    refresh_crm: false,
    review_view: 'all',
    save: false,
  })
  assert.equal(buildLeadControlSearchRequest('closed', '').manager_id, undefined)
  assert.equal(buildLeadControlSearchRequest('closed', '', true).refresh_crm, true)
})

test('lead selection removes checked items and stops at ten', () => {
  const selected = Array.from({ length: MAX_LEAD_CONTROL_SELECTION }, (_, index) => String(index + 1))
  assert.deepEqual(toggleLeadSelection(selected, '11'), selected)
  assert.deepEqual(toggleLeadSelection(selected, '10'), selected.slice(0, -1))
  assert.deepEqual(toggleLeadSelection(selected.slice(0, -1), '11'), [...selected.slice(0, -1), '11'])
})
