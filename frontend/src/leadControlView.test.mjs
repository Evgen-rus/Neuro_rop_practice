import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  MAX_LEAD_CONTROL_SELECTION,
  buildLeadControlSearchRequest,
  toggleLeadSelection,
  leadClosureLabel,
  leadCategoryLabel,
  leadTechnicalSummary,
  matchesLeadSearch,
} from './leadControlView.ts'

test('lead search matches title or id, ignores case and spaces, and allows an id with #', () => {
  const lead = { entity_id: '240579', title: 'Этикетировщик на пакет муки' }
  for (const query of ['', '  ', ' ЭТИКЕТИРОВЩИК ', 'пакет муки', '240579', '#240579', '0579']) {
    assert.equal(matchesLeadSearch(lead, query), true)
  }
  assert.equal(matchesLeadSearch(lead, '240580'), false)
  assert.equal(matchesLeadSearch(lead, 'насос'), false)
  assert.equal(matchesLeadSearch({ entity_id: '42', title: null }, 'насос'), false)
})

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

test('active leads never show a closure review badge, including stale closure verdicts', () => {
  for (const verdict of ['not_applicable', 'confirmed_correct', 'disputed', 'insufficient_evidence', null, undefined]) {
    assert.equal(leadClosureLabel('active', verdict), null)
  }
})

test('closed leads distinguish disputed closure from missing evidence and confirmation', () => {
  assert.equal(leadClosureLabel('closed', 'confirmed_correct'), 'Закрытие подтверждено')
  assert.equal(leadClosureLabel('closed', 'disputed'), 'Закрытие оспорено')
  assert.equal(leadClosureLabel('closed', 'insufficient_evidence'), 'Недостаточно данных для оценки')
  assert.equal(leadClosureLabel('closed', null), null)
})

test('lead assessment labels unknown categories and keeps technical counts with the timing qualifier', () => {
  assert.equal(leadCategoryLabel('unknown'), 'Не определена')
  assert.equal(leadCategoryLabel('  '), 'Не определена')
  assert.equal(leadCategoryLabel('B'), 'B')
  assert.equal(leadTechnicalSummary({
    technical_collected_count: 3,
    technical_total_count: 8,
    technical_partial_count: 1,
    technical_readiness: 'unknown',
    technical_required_now: true,
  }), 'ТЗ собрано 3 из 8 · частично 1 · тип оборудования не определён · требуется сейчас')
})
