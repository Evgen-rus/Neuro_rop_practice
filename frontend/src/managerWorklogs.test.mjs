import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import {
  commentsWithoutWorklogs,
  sortedWorklogEntries,
  toggleExpandedWorklog,
  visibleManagerWorklogs,
} from './managerWorklogs.ts'

function worklog(id, latest, changed = '2026-09-04T10:00:00+03:00') {
  return {
    is_worklog: true,
    comment_id: id,
    bitrix_created_at: '2026-07-28T10:00:00+03:00',
    author_id: '7',
    text: 'journal',
    content_hash: `hash-${id}`,
    entries: [],
    entry_count: 3,
    latest_entry_date: latest,
    first_seen_at: changed,
    last_changed_at: changed,
    last_seen_at: changed,
  }
}

test('worklogs stay above Bitrix history and duplicate comments are removed', () => {
  const item = worklog('old-comment', '2026-09-04')
  const comments = [
    { id: 'ordinary', created_at: '2026-08-07T10:00:00+03:00', text: 'Обычный', files: [] },
    { id: 'old-comment', created_at: '2026-07-28T10:00:00+03:00', text: 'Журнал', files: [] },
  ]
  assert.equal(visibleManagerWorklogs({ available: true, count: 1, items: [item] })[0].latest_entry_date, '2026-09-04')
  assert.deepEqual(commentsWithoutWorklogs(comments, [item]).map((comment) => comment.id), ['ordinary'])

  const source = readFileSync(new URL('./DealControl.tsx', import.meta.url), 'utf8')
  assert.ok(source.indexOf('className="dc-manager-worklogs"') < source.indexOf('className="dc-comments-pane-head"'))
  assert.match(readFileSync(new URL('./api.ts', import.meta.url), 'utf8'), /manager_worklogs: foreignProjection \? undefined/)
})

test('worklogs and their entries are sorted by internal recency', () => {
  const older = worklog('older', '2026-09-02')
  const newer = worklog('newer', '2026-09-04')
  assert.deepEqual(
    visibleManagerWorklogs({ available: true, count: 2, items: [older, newer] }).map((item) => item.comment_id),
    ['newer', 'older'],
  )
  assert.deepEqual(sortedWorklogEntries([
    { entry_date: '2026-09-02', date_raw: '02.09', text: 'second', year_inferred: true },
    { entry_date: '2026-09-04', date_raw: '04.09', text: 'latest', year_inferred: true },
    { entry_date: '2026-09-03', date_raw: '03.09', text: 'middle', year_inferred: true },
  ]).map((entry) => entry.entry_date), ['2026-09-04', '2026-09-03', '2026-09-02'])

  const changedLater = worklog('changed-later', '2026-09-04', '2026-09-04T12:00:00+03:00')
  assert.deepEqual(
    visibleManagerWorklogs({ available: true, count: 2, items: [newer, changedLater] }).map((item) => item.comment_id),
    ['changed-later', 'newer'],
  )
})

test('empty projection stays hidden and expansion state is independent', () => {
  assert.deepEqual(visibleManagerWorklogs(undefined), [])
  assert.deepEqual(visibleManagerWorklogs({ available: false, count: 1, items: [worklog('a', '2026-09-04')] }), [])
  assert.deepEqual(visibleManagerWorklogs({ available: true, count: 0, items: [] }), [])

  let expanded = toggleExpandedWorklog(new Set(), 'a')
  expanded = toggleExpandedWorklog(expanded, 'b')
  assert.deepEqual([...expanded].sort(), ['a', 'b'])
  expanded = toggleExpandedWorklog(expanded, 'a')
  assert.deepEqual([...expanded], ['b'])
})
