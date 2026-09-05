import type { DealTimelineComment, ManagerWorklog, ManagerWorklogEntry, ManagerWorklogsProjection } from './api'

function dateValue(value?: string | null) {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isNaN(parsed) ? 0 : parsed
}

export function visibleManagerWorklogs(projection?: ManagerWorklogsProjection): ManagerWorklog[] {
  if (!projection?.available || projection.count <= 0 || !projection.items.length) return []
  return [...projection.items].sort((left, right) =>
    dateValue(right.latest_entry_date) - dateValue(left.latest_entry_date)
    || dateValue(right.last_changed_at) - dateValue(left.last_changed_at)
    || dateValue(right.bitrix_created_at) - dateValue(left.bitrix_created_at)
    || String(right.comment_id || '').localeCompare(String(left.comment_id || ''))
  )
}

export function sortedWorklogEntries(entries: ManagerWorklogEntry[]): ManagerWorklogEntry[] {
  return [...entries].sort((left, right) =>
    dateValue(right.entry_date) - dateValue(left.entry_date)
    || right.date_raw.localeCompare(left.date_raw)
  )
}

export function managerWorklogPreview(projection?: ManagerWorklogsProjection) {
  const worklogs = visibleManagerWorklogs(projection)
  return {
    entries: sortedWorklogEntries(worklogs.flatMap((worklog) => worklog.entries || [])),
    entryCount: worklogs.reduce((total, worklog) => total + worklog.entry_count, 0),
  }
}

export function commentsWithoutWorklogs(
  comments: DealTimelineComment[],
  worklogs: ManagerWorklog[],
): DealTimelineComment[] {
  const worklogIds = new Set(worklogs.map((item) => String(item.comment_id || '')).filter(Boolean))
  return comments.filter((comment) => !worklogIds.has(String(comment.id)))
}

export function toggleExpandedWorklog(current: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(current)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  return next
}
